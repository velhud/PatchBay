import subprocess

from job_executor import JobExecutor
from job_manager import JobInfo, JobState


class DummyJobManager:
    def __init__(self, jobs):
        self.jobs = jobs

    def get_job(self, job_id):
        return self.jobs.get(job_id)


def make_config(tmp_path):
    return {
        "server": {"job_timeout_seconds": 30},
        "repositories": {"allowed": [str(tmp_path)]},
        "security": {
            "default_sandbox": "read-only",
            "allow_dangerously_bypass": False,
            "allowed_env_keys": ["PATH"],
            "max_diff_bytes": 10_000,
        },
        "logging": {"job_logs_dir": str(tmp_path / "logs")},
    }


def init_repo(repo):
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)


def make_job(repo, mode="apply", state=JobState.COMPLETED):
    return JobInfo(
        job_id="job-1",
        state=state,
        mode=mode,
        prompt="",
        repo_path=str(repo),
        worktree_path=str(repo),
    )


def test_get_diff_returns_redacted_tracked_apply_diff(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "README.md").write_text("hello\ntoken=fixture-value\n", encoding="utf-8")

    executor = JobExecutor(make_config(tmp_path), DummyJobManager({"job-1": make_job(repo)}))

    diff = executor.get_diff("job-1", "README.md")

    assert diff is not None
    assert "diff --git" in diff
    assert "token=[REDACTED_POSSIBLE_SECRET]" in diff
    assert "fixture-value" not in diff


def test_get_diff_returns_untracked_file_diff_only_when_status_changed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "new.txt").write_text("new line\n", encoding="utf-8")

    executor = JobExecutor(make_config(tmp_path), DummyJobManager({"job-1": make_job(repo)}))

    diff = executor.get_diff("job-1", "new.txt")

    assert diff is not None
    assert "--- /dev/null" in diff
    assert "+++ b/new.txt" in diff
    assert "+new line" in diff


def test_get_diff_does_not_return_full_unchanged_file_content(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)

    executor = JobExecutor(make_config(tmp_path), DummyJobManager({"job-1": make_job(repo)}))

    assert executor.get_diff("job-1", "README.md") is None


def test_get_diff_requires_completed_apply_job_and_safe_path(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    jobs = {
        "plan": make_job(repo, mode="plan"),
        "running": make_job(repo, state=JobState.RUNNING),
    }
    executor = JobExecutor(make_config(tmp_path), DummyJobManager(jobs))

    assert executor.get_diff("plan", "README.md") is None
    assert executor.get_diff("running", "README.md") is None
    assert executor.get_diff("missing", "README.md") is None
    assert executor.get_diff("plan", "../outside.txt") is None


def test_get_diff_is_bounded(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    (repo / "README.md").write_text("x" * 5000 + "\n", encoding="utf-8")
    config = make_config(tmp_path)
    config["security"]["max_diff_bytes"] = 80

    executor = JobExecutor(config, DummyJobManager({"job-1": make_job(repo)}))

    diff = executor.get_diff("job-1", "README.md")

    assert diff is not None
    assert "...[diff truncated to 80 bytes]" in diff
