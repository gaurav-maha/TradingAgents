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
            "--non-interactive",
            "--analysis-date",
            "2026-06-05",
            "--analysts",
            "market,news",
            "--research-depth",
            "1",
            "--output-language",
            "English",
        ],
    )

    assert result.exit_code == 0
    assert captured["universe_mode"] == "nyse_nasdaq_top"
    assert captured["universe_top_n"] == 17
    assert captured["universe_workers"] == 2
    assert captured["non_interactive"] is True
    assert captured["analysis_date"] == "2026-06-05"
    assert captured["analysts"] == "market,news"
    assert captured["research_depth"] == 1
    assert captured["output_language"] == "English"


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
    monkeypatch.setattr(main, "get_user_selections", lambda **kwargs: selections)

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
        non_interactive=True,
        analysis_date="2026-06-05",
        analysts="market,news",
        research_depth=1,
        output_language="English",
    )

    assert captured["trade_date"] == "2026-06-05"
    assert captured["selected_analysts"] == ["market", "news"]
    assert captured["limit"] == 17
    assert captured["workers"] == 2
    assert captured["config"]["checkpoint_enabled"] is False


def test_run_analysis_respects_zero_universe_limit(monkeypatch, tmp_path):
    import cli.main as main
    from cli.models import AnalystType
    from tradingagents.universe import UniverseSummary

    monkeypatch.setattr(
        main,
        "get_user_selections",
        lambda **kwargs: {
            "ticker": None,
            "asset_type": "stock",
            "analysis_date": "2026-06-05",
            "analysts": [AnalystType.MARKET],
            "research_depth": 1,
            "llm_provider": "codex",
            "backend_url": "https://chatgpt.com/backend-api/codex",
            "shallow_thinker": "gpt-5.5",
            "deep_thinker": "gpt-5.5",
            "google_thinking_level": None,
            "openai_reasoning_effort": None,
            "anthropic_effort": None,
            "output_language": "English",
        },
    )
    captured = {}

    def fake_universe_runner(**kwargs):
        captured.update(kwargs)
        return UniverseSummary(
            best_ticker=None,
            ranked_results=[],
            failed_results=[],
            output_dir=tmp_path,
        )

    monkeypatch.setattr(main, "run_top_nyse_nasdaq_universe", fake_universe_runner)

    main.run_analysis(
        universe_mode="nyse_nasdaq_top",
        universe_top_n=0,
        universe_workers=0,
        non_interactive=True,
    )

    assert captured["limit"] == 0
    assert captured["workers"] == 0


def test_non_interactive_universe_selection_uses_overrides_without_prompts(monkeypatch):
    import cli.main as main

    monkeypatch.setattr(main, "fetch_announcements", lambda: [])
    monkeypatch.setattr(main, "display_announcements", lambda console, announcements: None)

    prompt_names = [
        "get_analysis_date",
        "ask_output_language",
        "select_analysts",
        "select_research_depth",
        "select_llm_provider",
        "select_shallow_thinking_agent",
        "select_deep_thinking_agent",
    ]
    for name in prompt_names:
        monkeypatch.setattr(
            main,
            name,
            lambda *args, _name=name, **kwargs: (_ for _ in ()).throw(
                AssertionError(f"{_name} should not be called")
            ),
        )

    monkeypatch.setattr(
        main,
        "DEFAULT_CONFIG",
        {
            **main.DEFAULT_CONFIG,
            "llm_provider": "ollama",
            "backend_url": "http://localhost:11434/v1",
            "quick_think_llm": "llama3.1",
            "deep_think_llm": "llama3.1",
            "output_language": "English",
            "max_debate_rounds": 1,
        },
    )

    selections = main.get_user_selections(
        universe_mode="nyse_nasdaq_top",
        non_interactive=True,
        analysis_date="2026-06-05",
        analysts="market,news",
        research_depth=1,
        output_language="English",
    )

    assert selections["analysis_date"] == "2026-06-05"
    assert [analyst.value for analyst in selections["analysts"]] == ["market", "news"]
    assert selections["research_depth"] == 1
    assert selections["llm_provider"] == "ollama"
    assert selections["shallow_thinker"] == "llama3.1"
