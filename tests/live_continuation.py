"""Opt-in, synthetic-only Notion/model smoke acceptance; never run by unittest discovery.

plan is read-only; setup creates an isolated root; run calls the configured model
using only synthetic pages; cleanup archives only the recorded root. No daemon.
"""

import argparse
import json
import os
import time
from datetime import timedelta
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from orbit.config import Continuation, load_config
from orbit.engine import Engine, values
from orbit.model import Model
from orbit.notion import Notion, Conflict, UncertainWrite, paragraph, plain, rich, fingerprint, precise_page_date
from orbit.store import Store, encode
from orbit.thread_schema import ThreadSchema, KIND_LABELS, STATUS_LABELS
from orbit.threads import Threads
from orbit.timeutil import now, stamp
from orbit.transport import RemoteError
from tests.support import SCHEMAS


class LiveNotion(Notion):
    def __init__(self, config):
        super().__init__(config)
        self.last_request = 0
        self.lose_record_ack = False

    def request(self, method, path, body=None):
        delay = .36 - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        result = super().request(method, path, body)
        if self.lose_record_ack and method == 'POST' and path == 'pages':
            self.lose_record_ack = False
            raise RemoteError('Notion')
        return result


class ObservedModel(Model):
    def __init__(self, config, store):
        super().__init__(config)
        self.store = store

    def continue_thread(self, data):
        result = super().continue_thread(data)
        summary = {'at': stamp(now()), 'call_kind': data['call_kind'],
                   'thread_matches': result.get('thread_id') == data['thread_id'],
                   'revision_matches': result.get('expected_revision') == data['expected_revision'],
                   'expected_revision': data['expected_revision'],
                   'returned_revision_type': type(result.get('expected_revision')).__name__,
                   'returned_revision': result.get('expected_revision') if type(result.get('expected_revision')) is int else None,
                   'decision_kind': result.get('decision_kind'), 'outcome': result.get('outcome')}
        with self.store.db:
            self.store.put('model_checks', (self.store.get('model_checks', []) + [summary])[-100:])
        print(encode({'model_check': summary}), flush=True)
        return result


def create(store, notion, key, parent, title, text=None, properties=None):
    saved = store.get('setup:' + key, {})
    if saved.get('id'):
        return saved['id']
    kind = 'child_page' if properties is None else 'child_database'
    marker = 'Orbit continuation acceptance ' + store.get('acceptance_session') + ':' + key
    candidates = [b for b in notion.blocks(parent) if b['type'] == kind and b['text'] == title]
    if len(candidates) > 1:
        raise Conflict('隔离资源标识重复')
    if candidates:
        resource_id = candidates[0]['id']
        if kind == 'child_database':
            resource = notion.request('GET', 'databases/' + resource_id)
            if plain(resource.get('description', [])) != marker:
                raise Conflict('隔离数据库标识不匹配')
    else:
        if saved.get('sent'):
            raise UncertainWrite('隔离创建回执不明；不得盲目重复 setup')
        with store.db:
            store.put('setup:' + key, {'sent': True})
        try:
            if properties is None:
                resource = notion.request('POST', 'pages', {
                    'parent': {'type': 'page_id', 'page_id': parent}, 'properties': {'title': {'title': rich(title)}},
                    'children': [paragraph(marker), paragraph(text)]})
            else:
                resource = notion.request('POST', 'databases', {
                    'parent': {'type': 'page_id', 'page_id': parent}, 'title': rich(title), 'description': rich(marker),
                    'is_inline': True, 'initial_data_source': {'properties': properties}})
        except RemoteError as error:
            if error.status and 400 <= error.status < 500 and error.status not in {408, 409}:
                with store.db:
                    store.put('setup:' + key, {'sent': False, 'rejected_http_status': error.status})
            raise
        resource_id = resource['id']
    with store.db:
        store.put('setup:' + key, {'sent': True, 'id': resource_id})
    return resource_id


def isolated_config(config, store):
    pages, sources = store.get('test_pages'), store.get('test_sources')
    if not pages or not sources or not sources.get('threads'):
        raise Conflict('请先完成 setup；不允许退回正式资源')
    normalized = lambda items: {v.replace('-', '') for v in items}
    if normalized(pages.values()) & normalized(config.pages.values()) or normalized(sources.values()) & normalized(config.sources.values()):
        raise Conflict('隔离资源与正式配置重叠')
    return replace(config, pages=pages, sources=sources, continuation=Continuation(enabled=True))


def setup(config, store, notion):
    session = store.get('acceptance_session')
    if not session:
        session = uuid4().hex[:12]
        with store.db:
            store.put('acceptance_session', session)
    root = create(store, notion, 'root', config.pages['operations'], 'Orbit 后续链隔离验收｜' + session,
                  '技术验收临时区：全部虚构，不属于正式 Echo 生活历史。')
    with store.db:
        store.put('test_root', root)
    pages = store.get('test_pages', {})
    for name in config.pages:
        if name not in pages:
            body = {
                'identity': '纯虚构测试角色 Echo 是书店编辑；糖糖是虚构同事。可以自主接受、拒绝、追问或沉默；不存在真实人物消息。',
                'rules': '上海现实时间推进；不能补剧情；客观事件不得预设Echo感受。未知日期可自行询问，未来意图不是执行结果；技术测试不是生活。',
                'memory_rules': '只提炼有持续价值的共同叙事；保护人工原文；不生成现实人物经历和敏感信息。',
            }.get(name, '纯虚构隔离资料。人工原文保留。旧状态不等于现在；没有未记录经历。')
            pages[name] = create(store, notion, name, root, '虚构验收｜' + name + '｜' + session, body)
            with store.db:
                store.put('test_pages', pages)
    sources = store.get('test_sources', {})
    for name in ['memories', 'events', 'plans']:
        if name in sources:
            continue
        properties = json.loads(encode(SCHEMAS[name]))
        for field_name, spec in properties.items():
            if spec['type'] not in {'select', 'multi_select', 'relation'}:
                spec[spec['type']] = {}
            if spec['type'] == 'relation':
                target = sources['events' if field_name == '来源事件' else 'memories']
                spec['relation'] = {'data_source_id': target, 'type': 'single_property', 'single_property': {}}
        db_id = create(store, notion, 'source:' + name, root, '虚构数据库｜' + name + '｜' + session, properties=properties)
        db = notion.request('GET', 'databases/' + db_id)
        if len(db['data_sources']) != 1:
            raise Conflict('隔离数据源不唯一')
        sources[name] = db['data_sources'][0]['id']
        with store.db:
            store.put('test_sources', sources)
    cfg = replace(config, pages=pages, sources=sources)
    result = ThreadSchema(LiveNotion(cfg), store).apply()
    sources['threads'] = result['threads_source_id']
    with store.db:
        store.put('test_sources', sources)
    return {'setup_verified': True, 'root': root, 'synthetic_only': True}


def exercise(config, store, lost_ack=False, recover=True):
    cfg = isolated_config(config, store)
    notion = LiveNotion(cfg)
    engine = Engine(cfg, store, notion, ObservedModel(cfg, store), now)
    current = stamp(now())
    if recover:
        store.recover(current)
    engine.continuation.check_schema()
    # Suppress unrelated normal-life/root checks in this bounded probe.
    with store.db:
        store.put('continuation_life_at', current)
    if not store.get('continuation_enabled_at'):
        engine.continuation.poll(current)
    if not store.get('scenario_input_staged'):
        run = engine.continuation.run(current)
        operation = {'kind': 'record', 'source': 'events', 'values': {
            '事件': '虚构同事提出出游邀请', '一句话概要': '糖糖询问Echo是否愿意改天去附近看展，具体日期与地点尚未知。',
            '状态': '待处理', '信息性质': '共同叙事', '发生时间': current, '参与者': ['糖糖'], '来源与记录者': '隔离虚构测试输入'},
            'text': encode({'time': current, 'executor': 'Orbit', 'synthetic': True, 'unknowns': ['日期', '地点']})}
        with store.db:
            store.stage(run, [operation])
            store.put('scenario_input_staged', True)
    elif not store.get('scenario_supplement_staged'):
        waiting = [t for t in Threads(store).list() if t['status'] == 'awaiting_world' and t['notion_page_id']]
        if waiting:
            run = engine.continuation.run(current)
            op = {'kind': 'record', 'source': 'events', 'values': {
                '事件': '虚构同事后续补充', '一句话概要': '糖糖后来补充：周末可先商量日期，地点仍未决定，不要求Echo接受。',
                '状态': '待处理', '信息性质': '共同叙事', '发生时间': current,
                '所属事项': [waiting[0]['notion_page_id']], '参与者': ['糖糖'], '来源与记录者': '隔离虚构补充输入'},
                'text': encode({'time': current, 'executor': 'Orbit', 'synthetic': True, 'information_nature': '共同叙事'})}
            with store.db:
                store.stage(run, [op])
                store.put('scenario_supplement_staged', True)
    notion.lose_record_ack = lost_ack
    result = engine.tick()
    summary = {'at': stamp(now()), 'tick_result': result, 'threads': Threads(store).list(),
               'writes_pending': len(store.pending()), 'synthetic_only': True,
               'semantic_acceptance': '需人工检查自主性、未知项、角色职责及时间；本脚本不强制追问、接受或完成。',
               'uncovered': ['多轮世界无回复', '所有计划结果分支', '所有回执边界', '长期运行', '生产部署']}
    with store.db:
        store.put('last_live_report', summary)
    return summary


def audit(config, store):
    cfg = isolated_config(config, store)
    notion, repo = LiveNotion(cfg), Threads(store)
    ThreadSchema(notion, store).check()
    pages_by_id = {}
    markers = {}
    counts = {}
    for source in ['events', 'plans', 'memories', 'threads']:
        pages = [notion.page(row['id']) for row in notion.rows(source)]
        counts[source] = len(pages)
        for page in pages:
            pages_by_id[page['id']] = page
            if page['blocks'] and page['blocks'][0]['text'].startswith('orbit:'):
                key = page['blocks'][0]['text'].rsplit(':', 1)[0]
                markers.setdefault(key, []).append(page['id'])
            if values(page).get('事件') == '虚构同事提出出游邀请':
                if values(page).get('一句话概要') != '糖糖询问Echo是否愿意改天去附近看展，具体日期与地点尚未知。':
                    raise Conflict('审计：原邀请的未知项被改写')
    checks = []
    decisions = []
    for thread in repo.list():
        nodes = repo.nodes(thread['id'])
        page = pages_by_id[thread['notion_page_id']]
        if values(page)['处理进度'] != STATUS_LABELS[thread['status']] or thread['published_revision'] != thread['revision'] or thread['projection_dirty']:
            raise Conflict('审计：事项进度尚未核对')
        node_pages = {n['id']: n['notion_page_id'] for n in nodes}
        for node in nodes:
            actual = pages_by_id[node['notion_page_id']]
            if not node['verified_at'] or fingerprint(actual) != node['authority_version']:
                raise Conflict('审计：节点回执或来源版本不一致')
            if values(actual).get('所属事项') != [thread['notion_page_id']] or values(actual).get('节点类型') != KIND_LABELS[node['kind']]:
                raise Conflict('审计：节点事项/类型不一致')
            if stamp(precise_page_date(actual, '发生时间')) != node['occurred_at']:
                raise Conflict('审计：完整故事时间不一致')
            if node['kind'] != 'external_event' and node['actor'] != 'Echo':
                raise Conflict('审计：主观节点职责错误')
            if node['kind'] == 'external_event' and (values(actual).get('Echo的行动决定') or values(actual).get('Echo的理解与感受')):
                raise Conflict('审计：外部节点带有主观判断')
            if node['kind'] == 'echo_decision':
                decisions.append({'thread_id': thread['id'], 'decision_kind': json.loads(node['metadata']).get('decision_kind'),
                                  'decision': values(actual).get('Echo的行动决定'), 'needed_information': json.loads(node['metadata']).get('needed_information')})
        for edge in repo.summary(thread['id'])['edges']:
            target = pages_by_id[node_pages[edge['to_node_id']]]
            if edge['relation'] != 'supersedes' and node_pages[edge['from_node_id']] != target['id'] and node_pages[edge['from_node_id']] not in (values(target).get('前序事件') or []):
                raise Conflict('审计：因果前序关系缺失')
        checks.append({'thread_id': thread['id'], 'nodes': len(nodes), 'revision': thread['revision'], 'status': thread['status']})
    if any(len(ids) != 1 for ids in markers.values()):
        raise Conflict('审计：稳定写标识对应重复页面')
    if store.pending():
        raise Conflict('审计：仍有未核对写入')
    report = {'at': stamp(now()), 'remote_audit_passed': True, 'resources': counts,
              'threads': checks, 'decisions': decisions, 'unique_write_markers': len(markers),
              'model_checks': store.get('model_checks', []), 'synthetic_only': True}
    with store.db:
        store.put('last_remote_audit', report)
    return report


def stage_plan_scenario(config, store):
    cfg = isolated_config(config, store)
    if store.get('short_plan_input_staged'):
        return {'already_staged': True}
    notion = LiveNotion(cfg)
    engine = Engine(cfg, store, notion, ObservedModel(cfg, store), now)
    current = stamp(now())
    starts = stamp(now() + timedelta(minutes=3))
    detail = ('虚构书店的糖糖提出一个可自行决定是否参与的小安排：'
              + starts + ' 开始，在当前书桌旁花一分钟核对两张书单的一处书名。'
              '纸质目录和两张书单都在手边，之后没有其他已知约束。不涉及现实人物或现实任务。')
    operation = {'kind': 'record', 'source': 'events', 'values': {
        '事件': '虚构书单核对邀请', '一句话概要': detail, '状态': '待处理', '信息性质': '共同叙事',
        '发生时间': current, '参与者': ['糖糖'], '来源与记录者': '隔离虚构测试输入'},
        'text': encode({'time': current, 'executor': 'Orbit', 'synthetic': True, 'detail': detail})}
    with store.db:
        store.stage(engine.continuation.run(current), [operation])
        store.put('short_plan_input_staged', {'starts': starts})
    engine.flush()
    return {'staged': True, 'starts': starts, 'echo_choice_prescribed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['plan', 'setup', 'run', 'continue', 'scenario-plan', 'audit', 'cleanup'])
    parser.add_argument('--config', default='orbit.toml')
    parser.add_argument('--state', default='work/continuation-live.sqlite3')
    parser.add_argument('--lost-ack', action='store_true', help='模拟一次真实创建提交后的回执丢失')
    args = parser.parse_args(argv)
    os.umask(0o077)
    config = load_config(args.config)
    if Path(args.state).resolve() == Path(config.db_path).resolve():
        raise Conflict('验收state不得使用正式运行数据库')
    config.require_connections(model=args.stage in {'run', 'continue'})
    with_store = Store(args.state)
    try:
        with with_store.lock():
            if with_store.get('test_archived') and args.stage != 'cleanup':
                raise Conflict('隔离区已归档；新验收使用新的专用state路径')
            if args.stage == 'plan':
                if with_store.get('test_sources', {}).get('threads'):
                    result = ThreadSchema(LiveNotion(isolated_config(config, with_store)), with_store).plan()
                else:
                    LiveNotion(config).request('GET', 'pages/' + config.pages['operations'])
                    result = {'writes': False, 'setup_parent': config.pages['operations'], 'creates': '一个隔离根、七个虚构页面、四个隔离数据源；不外发正式正文'}
            elif args.stage == 'setup':
                result = setup(config, with_store, LiveNotion(config))
            elif args.stage in {'run', 'continue'}:
                result = exercise(config, with_store, args.lost_ack, recover=args.stage == 'run')
            elif args.stage == 'audit':
                result = audit(config, with_store)
            elif args.stage == 'scenario-plan':
                result = stage_plan_scenario(config, with_store)
            else:
                root = with_store.get('test_root')
                if not root or root.replace('-', '') in {v.replace('-', '') for v in config.pages.values()} or root != with_store.get('setup:root', {}).get('id'):
                    raise Conflict('没有可归档的隔离根')
                notion = LiveNotion(config)
                notion.request('PATCH', 'pages/' + root, {'archived': True})
                response = notion.request('GET', 'pages/' + root)
                if not (response.get('archived') or response.get('in_trash')):
                    raise Conflict('隔离区归档未核对')
                with with_store.db:
                    with_store.put('test_archived', True)
                result = {'archived': True, 'root': root, 'local_state': str(Path(args.state).resolve()),
                          'notice': '保留最后的脱敏报告后可删除本任务state数据库及其锁；不要删除正式运行数据库。'}
            print(encode(result))
    finally:
        with_store.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # No raw model/server response or configuration values.
        print(encode({'error_code': getattr(error, 'code', type(error).__name__), 'passed': False,
                      'service': getattr(error, 'service', None), 'http_status': getattr(error, 'status', None),
                      'reason': getattr(error, 'reason', None)}))
        raise SystemExit(1)
