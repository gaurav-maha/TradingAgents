from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner


def test_analyze_command_accepts_universe_options(monkeypatch):
    import cli.main as main

    captured = {}

    def fake_run_analysis(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(main, "run_analysis", fake_run_analysis)

    result = CliRunner().invoke(
        main.app,
        [
            "analyze",
            "--universe",
            "nyse_nasdaq_top",
            "--universe-limit",
            "17",
            "--universe-workers",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert captured["universe_mode"] == "nyse_nasdaq_top"
    assert captured["universe_top_n"] == 17
    assert captured["universe_workers"] == 2


def test_run_analysis_delegates_to_universe_batch(monkeypatch, tmp_path):
    import cli.main as main
    from cli.models import AnalystType
    from tradingagents.universe import UniverseSummary

    selections = {
        "ticker": None,
        "asset_type": "stock",
        "analysis_date": "2026-06-05",
        "analysts": [AnalystType.MARKET, AnalystType.NEWS],
        "research_depth": 1,
        "llm_provider": "ollama",
        "backend_url": "http://localhost:11434/v1",
        "shallow_thinker": "llama3.1",
        "deep_thinker": "llama3.1",
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "output_language": "English",
    }
    captured = {}

    monkeypatch.setattr(
        main,
        "DEFAULT_CONFIG",
        {
            **main.DEFAULT_CONFIG,
            "results_dir": str(tmp_path),
            "universe_mode": "nyse_nasdaq_top",
            "universe_top_n": 5000,
            "universe_workers": 8,
        },
    )
    monkeypatch.setattr(main, "get_user_selections", lambda universe_mode=None: selections)

    def fake_universe_runner(**kwargs):
        captured.update(kwargs)
        return UniverseSummary(
            best_ticker="MSFT",
            ranked_results=[],
            failed_results=[],
            output_dir=Path(tmp_path),
        )

    monkeypatch.setattr(main, "run_top_nyse_nasdaq_universe", fake_universe_runner)

    main.run_analysis(
        checkpoint=False,
        universe_mode="nyse_nasdaq_top",
        universe_top_n=17,
        universe_workers=2,
    )

    assert captured["trade_date"] == "2026-06-05"
    assert captured["selected_analysts"] == ["market", "news"]
    assert captured["limit"] == 17
    assert captured["workers"] == 2
    assert captured["config"]["checkpoint_enabled"] is False
