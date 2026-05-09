"""Tests for the log parser."""
from src.parsers.log_parser import parse_line, parse_logs


def test_json_line():
    line = '{"timestamp":"2026-04-12T18:01:02Z","level":"ERROR","source":"checkout","message":"db pool exhausted"}'
    ev = parse_line(line)
    assert ev is not None
    assert ev.level == "ERROR"
    assert ev.source == "checkout"
    assert "db pool" in ev.message


def test_syslog_line():
    line = "2026-04-12T14:22:16Z ERROR payment-service: panic: nil pointer"
    ev = parse_line(line)
    assert ev is not None
    assert ev.level == "ERROR"
    assert ev.source == "payment-service"
    assert "panic" in ev.message


def test_empty_line_returns_none():
    assert parse_line("") is None
    assert parse_line("   \n") is None


def test_fallback_for_unstructured():
    line = "Some unstructured log without timestamp"
    ev = parse_line(line)
    assert ev is not None
    assert ev.message == "Some unstructured log without timestamp"


def test_parse_logs_skips_blanks():
    text = "line1\n\n\nline2\n"
    events = parse_logs(text)
    assert len(events) == 2
