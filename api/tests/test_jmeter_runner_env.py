from app.services import jmeter_runner


def test_jmeter_environment_discards_missing_java_home(monkeypatch):
    monkeypatch.setenv("JAVA_HOME", "/missing/java")
    monkeypatch.setenv("JRE_HOME", "/missing/jre")

    env = jmeter_runner.jmeter_subprocess_env()

    assert "JAVA_HOME" not in env
    assert "JRE_HOME" not in env


def test_jmeter_environment_keeps_valid_java_home(tmp_path, monkeypatch):
    java_home = tmp_path / "jdk"
    java_binary = java_home / "bin" / "java"
    java_binary.parent.mkdir(parents=True)
    java_binary.write_text("")
    monkeypatch.setenv("JAVA_HOME", str(java_home))
    monkeypatch.delenv("JRE_HOME", raising=False)

    env = jmeter_runner.jmeter_subprocess_env()

    assert env["JAVA_HOME"] == str(java_home)
