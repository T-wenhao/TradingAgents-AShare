import ast
from pathlib import Path


MAIN_PATH = Path(__file__).resolve().parents[1] / "api" / "main.py"


def _module_assignments():
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    return {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def test_default_job_timeout_is_longer_than_legacy_ten_minutes():
    assignments = _module_assignments()
    default_timeout = ast.literal_eval(assignments["DEFAULT_JOB_TIMEOUT_SECONDS"])

    assert default_timeout == 1800
    assert default_timeout > 600


def test_job_timeout_uses_ta_job_timeout_env_var():
    assignments = _module_assignments()
    job_timeout = assignments["_JOB_TIMEOUT"]

    assert isinstance(job_timeout, ast.Call)
    assert isinstance(job_timeout.func, ast.Name)
    assert job_timeout.func.id == "int"

    getenv_call = job_timeout.args[0]
    assert isinstance(getenv_call, ast.Call)
    assert isinstance(getenv_call.func, ast.Attribute)
    assert isinstance(getenv_call.func.value, ast.Name)
    assert getenv_call.func.value.id == "os"
    assert getenv_call.func.attr == "getenv"
    assert ast.literal_eval(getenv_call.args[0]) == "TA_JOB_TIMEOUT"
    assert "DEFAULT_JOB_TIMEOUT_SECONDS" in ast.unparse(getenv_call.args[1])
