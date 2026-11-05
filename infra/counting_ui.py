"""Focused offline counting lab; existing capture/annotation pages stay reusable."""
import json
from itertools import product
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from beatdance.counting import PRESETS, BASELINE, DEFAULTS
from beatdance.signal_processing import AlgorithmConfig
from .experiments import ExperimentConfig
from .counting_lab import BatchJob, load_comparison, paired_cases, summary_rows, overlay, meeting_report
from .store import read_json, write_json, new_id


def counting_page(store):
    st.subheader("离线计数算法实验台")
    st.caption("同一数据对比检测规则。所有预置共用离线零相位预处理；绿线为人工动作。合成数据仅检验流程。")
    datasets = store.list_objects("dataset")
    if not datasets:
        st.info("先到数据页导入真实记录并建立标注数据集；也可创建合成流程示例验证操作。")
        return
    batch = st.session_state.get("counting_batch")
    active = bool(batch and batch.thread.is_alive())
    with st.form("counting_batch_form"):
        dataset = st.selectbox("实验数据集", [d["id"] for d in datasets], format_func=lambda v: next(d["name"] for d in datasets if d["id"] == v))
        question = st.text_input("本次想验证的问题", "自适应规则能否减少漏计，同时控制误检？")
        purpose = st.selectbox("数据用途（自行按完整会话划分）", ["development", "holdout"])
        algorithms = st.multiselect("候选算法", list(PRESETS), default=[BASELINE, "adaptive-amplitude-v1", "adaptive-interval-v1", "hysteresis-v1"], format_func=PRESETS.get)
        common = st.text_area("公共预处理与固定参数 JSON", json.dumps(AlgorithmConfig().to_dict(), indent=2))
        st.caption("prominence 仅作用于固定幅度方案；min_peak_interval 仅作用于固定间隔方案。自适应规则由下面的参数控制。")
        custom = st.text_area("自适应／状态机参数 JSON", json.dumps(DEFAULTS, indent=2))
        tolerance = st.number_input("统一匹配容差 / s", min_value=0.001, value=0.1)
        sweep_key = st.selectbox("可选参数扫描", ["无", "prominence", "min_peak_interval", "amplitude_factor", "interval_ratio", "high_sigma"])
        sweep_values = st.text_input("扫描取值 JSON 数组", "[0.3, 0.4, 0.5]")
        submitted = st.form_submit_button("批量运行", disabled=active)
    if submitted:
        try:
            if not algorithms:
                raise ValueError("至少选择一个算法")
            if purpose == "holdout" and sweep_key != "无":
                raise ValueError("参数扫描用于开发集；留出验证请使用已确定的配置")
            base, params = json.loads(common), json.loads(custom)
            values = [None] if sweep_key == "无" else json.loads(sweep_values)
            if not isinstance(values, list):
                raise ValueError("扫描取值必须是数组")
            configs = []
            for algorithm, value in product(algorithms, values):
                a, p = dict(base), dict(params)
                if sweep_key in a:
                    a[sweep_key] = value
                elif sweep_key != "无":
                    p[sweep_key] = value
                configs.append(ExperimentConfig(AlgorithmConfig(**a), tolerance, algorithm, p))
            st.session_state.counting_batch = BatchJob(store, dataset, configs, question, purpose).start()
            st.rerun()
        except (ValueError, TypeError) as exc:
            st.error(str(exc))
    if batch:
        st.json(batch.snapshot())
        if st.button("停止批量实验", disabled=not active):
            batch.cancel()
        st.button("刷新批量进度")
        st.caption("切页不停止批次；停止请求在片段边界生效。进程退出后可在实验历史查看已完成结果。")
    histories = sorted((store.runs.parent / "counting_batches").glob("*/batch.json"), reverse=True)
    if histories:
        with st.expander("批次历史与实验问题"):
            for path in histories:
                st.json(read_json(path))
    runs = [r for r in store.list_objects("run") if r["status"] == "completed"]
    chosen = st.multiselect("同数据对比", [r["id"] for r in runs], default=[v for v in (batch.snapshot()["runs"] if batch and not active else []) if v in {r["id"] for r in runs}][:6])
    if not chosen:
        return
    try:
        compared = load_comparison(store, chosen)
    except (ValueError, OSError) as exc:
        st.warning(str(exc))
        return
    table = pd.DataFrame(summary_rows(compared))
    st.dataframe(table, hide_index=True)
    st.download_button("下载对比表", table.to_csv(index=False).encode("utf-8-sig"), "comparison.csv")
    st.caption("误检与漏检分别查看；not_evaluated 表示无评分依据。时间误差为离线事件定位误差。")
    with st.expander("各会话／片段结果与配置"):
        for run in compared:
            st.write(run["id"])
            st.json(run["config"])
            detail = pd.json_normalize(run["metrics"]["selections"])
            st.dataframe(detail, hide_index=True)
    first = compared[0]["analysis"]
    if first.empty:
        st.info("没有可展示的样本。")
        return
    selection = st.selectbox("同步查看片段", [int(i) for i in first.selection.unique()])
    part = first[first.selection == selection]
    focus = None
    if len(compared) >= 2:
        st.caption("配对差异以所选前两个实验为 A/B；误检独立列出，不推断同一误检的作者或对应关系。")
        paired = paired_cases(compared[0]["cases"], compared[1]["cases"])
        differences = paired[(paired.selection == selection) & (paired.change != "both matched")]
        st.dataframe(differences, hide_index=True)
        choices = [None] + differences.truth_time.tolist()
        focus = st.selectbox("定位动作差异", choices, format_func=lambda x: "手动时间窗" if x is None else f"{x:.3f} s")
    errors = []
    for run in compared:
        c = run["cases"]
        errors.append(c[(c.selection == selection) & (c.kind != "matched")].assign(run=run["id"]))
    error_table = pd.concat(errors, ignore_index=True)
    with st.expander("全部误检／漏检"):
        st.dataframe(error_table, hide_index=True)
        error_focus = st.selectbox("定位错误", [None]+list(error_table.index), format_func=lambda i: "不定位" if i is None else f"{error_table.loc[i, 'run']} {error_table.loc[i, 'kind']} @ {error_table.loc[i, 'time']:.3f}s")
        if error_focus is not None:
            focus = float(error_table.loc[error_focus, "time"])
    low, high = float(part.time.min()), float(part.time.max())
    if high <= low:
        return
    start, end = st.slider("共同时间窗 / s", low, high, (low, high), key=f"lab_window_{selection}_{chosen[0]}")
    if focus is not None:
        start, end = max(low, focus-1.5), min(high, focus+1.5)
    display = st.radio("复核显示", ["视频同步对比", "静态波形对比"], horizontal=True)
    if display == "视频同步对比":
        from .video_review import comparison_video_panel
        comparison_video_panel(compared, selection, focus)
    else:
        fig = overlay(compared, selection, start, end)
        st.pyplot(fig)
        plt.close(fig)
    with st.expander("候选点接受／拒绝原因"):
        for run in compared:
            path = run["directory"] / "inputs" / str(selection) / "candidates.csv"
            if path.exists():
                candidates = pd.read_csv(path)
                st.write(run["id"])
                st.dataframe(candidates[candidates.time.between(start, end)], hide_index=True)
    conclusion = st.text_area("组会结论与已知限制")
    if st.button("生成组会报告"):
        report = meeting_report(compared, batch.snapshot()["question"] if batch else "", conclusion)
        review_dir = store.runs.parent / "counting_reviews" / new_id("review")
        review_dir.mkdir(parents=True, exist_ok=False)
        write_json(review_dir / "review.json", dict(runs=chosen, conclusion=conclusion))
        (review_dir / "report.html").write_text(report, encoding="utf-8")
        st.download_button("下载独立 HTML 报告", report.encode("utf-8"), "counting_comparison.html", mime="text/html")
        st.caption(f"报告与结论已保存：{review_dir}")
