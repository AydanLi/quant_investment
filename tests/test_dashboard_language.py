import re
from types import SimpleNamespace

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import streamlit_dashboard_db_v1_1_save_experiment as dashboard
from services.dashboard_i18n import localize_frame, translate, translate_warning


APP_SCRIPT = (
    "import streamlit_dashboard_db_v1_1_save_experiment as dashboard\n"
    "dashboard.main()\n"
)
CJK = re.compile(r"[\u4e00-\u9fff]")
ADMITTED_MODEL = {
    "risk_model": "dynamic_factor",
    "ewma_half_life_days": 40,
    "pca_stress_multiplier": 1.5,
    "admission_label": "dynamic_half_life_40_stress_1.5",
}


def _stored_results():
    dates = pd.date_range("2024-01-02", periods=3)
    runs = pd.DataFrame(
        [
            {"id": run_id, "scenario_name": f"saved_{run_id}", "cagr": 0.12,
             "sharpe": 1.2, "sortino": 1.5, "max_drawdown": -0.08,
             "annual_vol": 0.10, "avg_turnover": 0.03,
             "rebalance_frequency": "M", "top_n": 3, "latest_regime": "risk_on",
             "config_json": {"risk_model": "sample"}}
            for run_id in (12, 11)
        ]
    )
    portfolio = pd.DataFrame(
        {"date": dates, "equity": [100.0, 101.0, 102.0],
         "daily_return": [0.0, 0.01, 0.0099], "regime": ["risk_on"] * 3}
    )
    orders = pd.DataFrame({"ticker": ["SPY"], "side": ["BUY"], "quantity": [2]})
    signals = pd.DataFrame({"ticker": ["SPY"], "weight": [0.35]})
    factors = SimpleNamespace(
        rolling_summary={"oos_r_squared": 0.55, "observations": 300},
        static_regression=SimpleNamespace(
            coefficients={"alpha": 0.0001}, t_statistics={"alpha": 1.0},
            variance_contribution={"residual": 0.20},
        ),
        rolling_attribution=SimpleNamespace(
            exposures=pd.DataFrame(0.25, index=dates, columns=dashboard.FACTOR_LABELS)
        ),
        exposure_table=pd.DataFrame(
            {"因子": ["市场"], "最新暴露": [0.8], "历史10%": [0.2],
             "历史中位数": [0.4], "历史90%": [0.6], "状态": ["高于历史90%分位"]}
        ),
        return_contribution=pd.Series({"cash": 0.02, "alpha": 0.01, "equity_market": 0.08}),
        risk_contribution=pd.Series({"equity_market": 0.8, "residual": 0.2}),
        status="watch",
        warnings=("市场暴露 0.800 高于本次实验的历史90%分位。",),
    )
    monte_carlo = SimpleNamespace(
        horizon=252, observations=300, simulations=3000, block_length=20,
        probability_of_loss=0.30, tail_max_drawdown=-0.25, median_max_drawdown=-0.12,
        median_total_return=0.08, median_sharpe=1.0, median_turnover=2.0, median_cost=0.003,
        distribution_table=pd.DataFrame(
            {"指标": ["总收益", "Sharpe"], "5%": [-0.1, 0.5], "中位数": [0.08, 1.0],
             "95%": [0.25, 1.5], "单位": ["percent", "number"]}
        ),
        sensitivity_table=pd.DataFrame(
            {"区块长度": [20], "亏损概率": [0.30], "5%尾部回撤": [-0.25],
             "中位总收益": [0.08], "中位Sharpe": [1.0]}
        ),
        equity_quantiles=pd.DataFrame(
            {"5%路径": [1.0, 0.9], "中位路径": [1.0, 1.08], "95%路径": [1.0, 1.25]},
        ).rename_axis("交易日"),
        status="watch",
        warnings=("未来252日模拟亏损概率达到 30.0%。",),
    )
    return runs, portfolio, orders, signals, factors, monte_carlo


def _app(monkeypatch, *, populated=False, query=None):
    data = _stored_results()
    runs, portfolio, orders, signals, factors, monte_carlo = data
    monkeypatch.setattr(dashboard, "load_runs", lambda limit: runs if populated else pd.DataFrame())
    monkeypatch.setattr(dashboard, "load_admitted_dynamic_factor", lambda: ADMITTED_MODEL.copy())
    monkeypatch.setattr(dashboard, "load_run_details", lambda run_id: (portfolio, orders, signals))
    monkeypatch.setattr(dashboard, "load_factor_monitor", lambda run_id: factors)
    monkeypatch.setattr(dashboard, "load_monte_carlo_monitor", lambda run_id: monte_carlo)

    def forbidden(*args, **kwargs):
        pytest.fail("Rendering or changing dashboard language must not access storage or execute experiments")

    monkeypatch.setattr(dashboard, "ResearchStore", forbidden)
    monkeypatch.setattr(dashboard, "execute_experiment_and_save", forbidden)
    app = AppTest.from_string(APP_SCRIPT, default_timeout=30)
    app.query_params.update(query or {})
    return app, data


def _assert_no_exception(app):
    assert not app.exception, [exception.value for exception in app.exception]


def _visible_text(app):
    values = []
    for collection in (app.title, app.header, app.subheader, app.caption, app.markdown,
                       app.info, app.warning, app.error, app.success):
        values.extend(str(element.value) for element in collection)
    values.extend(tab.label for tab in app.tabs)
    values.extend(metric.label for metric in app.metric)
    for collection in (app.text_input, app.slider, app.number_input, app.checkbox, app.button):
        values.extend(widget.label for widget in collection)
    for widget in app.selectbox:
        if widget.key != "dashboard_language":
            values.append(widget.label)
            values.extend(widget.options)
    return "\n".join(values)


@pytest.mark.parametrize("query_language, expected", [("en", "en"), ("unsupported", "zh")])
def test_language_initializes_from_url_and_preserves_other_query_parameters(
    monkeypatch, query_language, expected
):
    app, _ = _app(monkeypatch, query={"lang": query_language, "view": "saved"})
    app.run()
    _assert_no_exception(app)
    assert app.selectbox(key="dashboard_language").value == expected
    assert app.query_params["lang"] == [expected]
    assert app.query_params["view"] == ["saved"]


def test_language_switch_works_without_saved_runs_and_survives_rerun(monkeypatch):
    app, _ = _app(monkeypatch)
    monkeypatch.setattr(dashboard, "load_admitted_dynamic_factor", lambda: None)
    app.run()
    _assert_no_exception(app)
    assert app.selectbox(key="dashboard_language").value == "zh"
    assert any("没有实验记录" in message.value for message in app.warning)

    app.selectbox(key="dashboard_language").set_value("en").run()
    _assert_no_exception(app)
    baseline_widget = app.selectbox(key="risk_model")
    assert baseline_widget.proto.set_value
    assert baseline_widget.proto.raw_value == baseline_widget.options[0]
    assert not CJK.search(_visible_text(app))
    assert len(app.warning) == 1
    app.run()
    _assert_no_exception(app)
    assert app.selectbox(key="dashboard_language").value == "en"
    assert app.query_params["lang"] == ["en"]

    refreshed, _ = _app(monkeypatch, query=app.query_params)
    refreshed.run()
    _assert_no_exception(refreshed)
    assert refreshed.selectbox(key="dashboard_language").value == "en"
    assert not CJK.search(_visible_text(refreshed))

    app.selectbox(key="dashboard_language").set_value("zh").run()
    _assert_no_exception(app)
    baseline_widget = app.selectbox(key="risk_model")
    assert baseline_widget.proto.set_value
    assert baseline_widget.proto.raw_value == baseline_widget.options[0]
    assert CJK.search(baseline_widget.proto.raw_value)


def test_populated_dashboard_switch_preserves_inputs_selected_run_and_source_data(monkeypatch):
    app, data = _app(monkeypatch, populated=True)
    originals = [frame.copy(deep=True) for frame in data[:4]]
    exposure_original = data[4].exposure_table.copy(deep=True)
    distribution_original = data[5].distribution_table.copy(deep=True)
    app.run()
    _assert_no_exception(app)
    assert len(app.tabs) == 6
    assert app.tabs[0].label == "净值曲线"

    risk_option = list(dashboard.dashboard_risk_model_options(ADMITTED_MODEL))[1]
    app.text_input(key="scenario_name").set_value("my_saved_choice")
    app.text_input(key="start_date").set_value("2020-01-02")
    app.selectbox(key="rebalance_frequency").set_value("W")
    app.selectbox(key="risk_model").set_value(risk_option)
    app.checkbox(key="use_auto_name").uncheck()
    app.selectbox(key="selected_run_id").set_value(11)
    app.number_input(key="top_n_input").set_value(4)
    app.slider(key="history_limit_slider").set_value(35)
    app.run()
    _assert_no_exception(app)

    for language in ("en", "zh"):
        app.selectbox(key="dashboard_language").set_value(language).run()
        _assert_no_exception(app)
        assert app.text_input(key="scenario_name").value == "my_saved_choice"
        assert app.text_input(key="start_date").value == "2020-01-02"
        assert app.selectbox(key="rebalance_frequency").value == "W"
        assert app.selectbox(key="risk_model").value == risk_option
        # The frontend needs an explicit value update when format_func changes;
        # preserved Python state alone can leave the selected text in the old language.
        for key in ("rebalance_frequency", "risk_model"):
            widget = app.selectbox(key=key)
            assert widget.proto.set_value
            assert widget.proto.raw_value == widget.options[1]
        assert app.checkbox(key="use_auto_name").value is False
        assert app.selectbox(key="selected_run_id").value == 11
        assert app.number_input(key="top_n_input").value == 4
        assert app.slider(key="top_n_slider").value == 4
        assert app.number_input(key="history_limit_input").value == 35
        assert app.slider(key="history_limit_slider").value == 35
        assert any(metric.value == "saved_11" for metric in app.metric)
        if language == "en":
            assert not CJK.search(_visible_text(app))
            assert all(not CJK.search(str(column)) for item in app.dataframe for column in item.value.columns)
            assert any("Equity market" in str(item.value) for item in app.dataframe)
        else:
            assert app.tabs[0].label == "净值曲线"

    for original, actual in zip(originals, data[:4]):
        pd.testing.assert_frame_equal(actual, original)
    pd.testing.assert_frame_equal(data[4].exposure_table, exposure_original)
    pd.testing.assert_frame_equal(data[5].distribution_table, distribution_original)


def test_validation_errors_change_language_and_keep_save_disabled(monkeypatch):
    app, _ = _app(monkeypatch)
    app.run()
    app.text_input(key="start_date").set_value("invalid-date").run()
    _assert_no_exception(app)
    assert any("YYYY-MM-DD" in message.value and CJK.search(message.value) for message in app.error)
    assert app.button(key="save_experiment").disabled

    app.selectbox(key="dashboard_language").set_value("en").run()
    _assert_no_exception(app)
    assert app.text_input(key="start_date").value == "invalid-date"
    assert any("YYYY-MM-DD" in message.value for message in app.error)
    assert not CJK.search("\n".join(message.value for message in app.error))
    assert app.button(key="save_experiment").disabled


@pytest.mark.parametrize("loader", ["load_runs", "load_run_details"])
def test_database_error_keeps_language_switch_available(monkeypatch, loader):
    app, _ = _app(monkeypatch, populated=True)

    def fail_read(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(dashboard, loader, fail_read)
    app.run()
    _assert_no_exception(app)
    assert any(CJK.search(message.value) and "database unavailable" in message.value for message in app.error)
    app.selectbox(key="dashboard_language").set_value("en").run()
    _assert_no_exception(app)
    assert any("database unavailable" in message.value for message in app.error)
    assert not CJK.search("\n".join(message.value for message in app.error))


def test_monitor_errors_are_localized_without_hiding_populated_results(monkeypatch):
    app, _ = _app(monkeypatch, populated=True, query={"lang": "en"})

    def fail_monitor(run_id):
        raise ValueError("insufficient observations")

    monkeypatch.setattr(dashboard, "load_factor_monitor", fail_monitor)
    monkeypatch.setattr(dashboard, "load_monte_carlo_monitor", fail_monitor)
    app.run()
    _assert_no_exception(app)
    assert len(app.tabs) == 6
    assert sum("insufficient observations" in message.value for message in app.warning) == 2
    assert not CJK.search(_visible_text(app))


def test_empty_run_details_show_localized_guidance_in_all_tabs(monkeypatch):
    app, _ = _app(monkeypatch, populated=True, query={"lang": "en"})
    monkeypatch.setattr(
        dashboard, "load_run_details", lambda run_id: (pd.DataFrame(),) * 3
    )
    app.run()
    _assert_no_exception(app)
    assert len(app.tabs) == 6
    assert len(app.info) >= 5
    assert not CJK.search(_visible_text(app))


@pytest.mark.parametrize("language", ["en", "zh"])
def test_unknown_provider_messages_are_preserved_including_braces(language):
    message = "Provider response {error}: {'code': 42}; custom message"
    assert translate(message, language, error="replacement") == message
    assert translate_warning(message, language) == message


def test_warning_translation_preserves_numeric_values_and_known_factor_name():
    assert translate_warning(
        "市场暴露 -0.123 低于本次实验的历史10%分位。", "en"
    ) == "Equity market exposure -0.123 is below this experiment's historical 10th percentile."
    assert translate_warning(
        "5%尾部路径最大回撤达到 -25.0%。", "en"
    ) == "The 5th-percentile path maximum drawdown has reached -25.0%."


def test_frame_translation_preserves_user_content_and_numeric_data():
    source = pd.DataFrame(
        {"scenario_name": ["市场"], "因子": ["市场"], "value": [0.35]},
        index=pd.Index(["市场"], name="date"),
    )
    original = source.copy(deep=True)
    translated = localize_frame(source, "en", value_columns=("因子",))

    assert list(translated.columns) == ["Scenario Name", "Factor", "value"]
    assert translated.index.name == "Date"
    assert translated.index[0] == "市场"
    assert translated.iloc[0].to_list() == ["市场", "Equity market", 0.35]
    pd.testing.assert_frame_equal(source, original)
