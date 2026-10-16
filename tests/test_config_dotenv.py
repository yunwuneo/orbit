import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from orbit.cli import main, parser
from orbit.config import ConfigError, load_config, read_dotenv


class DotenvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orbit-dotenv-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / "orbit.toml"
        self.config_path.write_text('[model]\nbase_url="https://toml.example/v1"\nname="toml-model"\n')
        self.env_path = self.root / ".env"
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_loads_env_beside_config_and_does_not_change_process_environment(self):
        self.env_path.write_text(
            "ORBIT_MODEL_BASE_URL=https://file.example/v1\n"
            "ORBIT_MODEL_NAME=file-model\n"
            "ORBIT_MODEL_API_KEY=test-model-credential\n"
            "ORBIT_NOTION_TOKEN=test-notion-credential\n")
        config = load_config(self.config_path)
        config.require_connections()
        self.assertEqual(config.model_base_url, "https://file.example/v1")
        self.assertEqual(config.model_name, "file-model")
        self.assertEqual(config.model_key, "test-model-credential")
        self.assertEqual(config.notion_token, "test-notion-credential")
        self.assertNotIn("test-model-credential", repr(config))
        self.assertNotIn("ORBIT_MODEL_API_KEY", os.environ)

    def test_environment_has_priority_including_explicit_empty_values(self):
        self.env_path.write_text("ORBIT_MODEL_NAME=file-model\nORBIT_MODEL_API_KEY=file-credential\n")
        with patch.dict(os.environ, {"ORBIT_MODEL_NAME": "process-model", "ORBIT_MODEL_API_KEY": ""}):
            config = load_config(self.config_path)
        self.assertEqual(config.model_name, "process-model")
        self.assertEqual(config.model_key, "")
        self.assertEqual(config.model_base_url, "https://toml.example/v1")

    def test_quotes_comments_export_bom_and_empty_values(self):
        self.env_path.write_text(
            "\ufeff# comment\n\nexport NAME = 'Echo 七七 # keep' # comment\n"
            'TOKEN="value # keep" # comment\nPLAIN=plain-value # comment\n'
            'HASH=value#keep\nEMPTY=\nSPACES="  keep spaces  "\n', encoding="utf-8")
        self.assertEqual(read_dotenv(self.env_path), {
            "NAME": "Echo 七七 # keep", "TOKEN": "value # keep", "PLAIN": "plain-value",
            "HASH": "value#keep", "EMPTY": "", "SPACES": "  keep spaces  ",
        })

    def test_double_quote_escapes_and_literal_single_quote_values(self):
        self.env_path.write_text(r'''DOUBLE="first\nsecond\t\"quoted\"\\end"
SINGLE='first\nsecond'
LITERAL=$(do-not-run)${HOME}
''')
        values = read_dotenv(self.env_path)
        self.assertEqual(values["DOUBLE"], 'first\nsecond\t"quoted"\\end')
        self.assertEqual(values["SINGLE"], r"first\nsecond")
        self.assertEqual(values["LITERAL"], "$(do-not-run)${HOME}")

    def test_missing_default_is_optional_but_explicit_path_is_required(self):
        self.assertEqual(load_config(self.config_path).model_name, "toml-model")
        with self.assertRaises(ConfigError):
            load_config(self.config_path, env_file=self.env_path)

    def test_explicit_env_file_replaces_default_file(self):
        self.env_path.write_text("ORBIT_MODEL_NAME=default-file\nORBIT_NOTION_TOKEN=default-token\n")
        alternate = self.root / "deployment.env"
        alternate.write_text("ORBIT_MODEL_NAME=alternate-file\n")
        config = load_config(self.config_path, env_file=alternate)
        self.assertEqual(config.model_name, "alternate-file")
        self.assertEqual(config.notion_token, "")
        args = parser().parse_args(["--env-file", str(alternate), "check"])
        self.assertEqual(args.env_file, str(alternate))

    def test_invalid_lines_never_reveal_the_value(self):
        for line in ["INVALID-KEY=private-test-value", 'TOKEN="private-test-value',
                     "TOKEN='private-test-value' unexpected", "private-test-value"]:
            with self.subTest(line=line):
                self.env_path.write_text("# first\n" + line + "\n")
                with self.assertRaises(ConfigError) as error:
                    load_config(self.config_path)
                self.assertIn("2", str(error.exception))
                self.assertNotIn("private-test-value", str(error.exception))

    def test_cli_reports_bad_env_without_values_or_database_creation(self):
        self.env_path.write_text('TOKEN="private-test-value\n')
        stderr, stdout = io.StringIO(), io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(stdout):
            result = main(["--config", str(self.config_path), "--env-file", str(self.env_path), "status"])
        self.assertEqual(result, 1)
        self.assertNotIn("private-test-value", stderr.getvalue() + stdout.getvalue())
        self.assertIn("引号格式无效", stderr.getvalue())

    def test_invalid_encoding_and_oversized_files_fail_safely(self):
        self.env_path.write_bytes(b"TOKEN=\xff")
        with self.assertRaises(ConfigError):
            read_dotenv(self.env_path)
        self.env_path.write_text("TOKEN=" + "x" * 131073)
        with self.assertRaises(ConfigError):
            read_dotenv(self.env_path)


if __name__ == "__main__":
    unittest.main()
