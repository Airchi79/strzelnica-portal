"""Harmonogram zadań okresowych w jednym procesie (kontener cron)."""

import pytest
from django.core.management import call_command

from apps.core.management.commands import run_scheduler


def test_tasks_for_ticks():
    assert run_scheduler.tasks_for(0) == run_scheduler.EVERY_MINUTE + run_scheduler.EVERY_5_MINUTES + run_scheduler.EVERY_HOUR
    assert run_scheduler.tasks_for(1) == run_scheduler.EVERY_MINUTE
    assert run_scheduler.tasks_for(5) == run_scheduler.EVERY_MINUTE + run_scheduler.EVERY_5_MINUTES
    assert run_scheduler.tasks_for(120)[-1] == "check_backups"


def test_every_task_is_a_real_command():
    from django.core.management import get_commands

    names = set(get_commands())
    missing = [n for n in run_scheduler.tasks_for(0) if n not in names]
    assert not missing, missing


@pytest.mark.django_db(transaction=True)
def test_failed_task_does_not_stop_the_others(monkeypatch):
    calls = []

    def fake(name, **kw):
        calls.append(name)
        if name == "process_waitlist":
            raise RuntimeError("awaria")

    monkeypatch.setattr(run_scheduler, "call_command", fake)
    call_command("run_scheduler", ticks=2, interval=0)
    assert calls == run_scheduler.tasks_for(0) + run_scheduler.tasks_for(1)


@pytest.mark.django_db(transaction=True)
def test_real_minute_tasks_run_in_one_process(caplog):
    call_command("run_scheduler", ticks=1, interval=0)  # obieg 0 — wszystkie zadania na pustej bazie, bez wyjątku
    assert "zakończone błędem" not in caplog.text, caplog.text
