"""Runnable diagnosis entry point.

Usage (from the repository root):

    .venv/bin/python -m investigation.diagnosis.run_demo

It re-executes every controlled scenario, prints the evidence chain in Chinese
and writes machine-readable samples under investigation/samples/. It starts
fresh workflows from the checked-in BPMN files; it never mutates the library.
"""

import json
from pathlib import Path

from . import scenarios as s
from . import harness as h

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "samples"


def _rows_text(view, key):
    return view[key]["data"]["total"]["rows"]


def _print_section(title):
    print("\n" + "#" * 78)
    print("# " + title)
    print("#" * 78)


def report_midpoint():
    _print_section("检查点一：甲调换完、乙未动 —— 公开数据 vs 存档能证明的事实")
    for case in h.CASES:
        record = s.midpoint_evidence(case)
        print(f"\n[{case}] 存档大小 {record['archive_bytes']} 字节")
        print(f"  公开数据（深拷贝，无地址）live 与存档恢复后完全一致: {record['public_data_identical']}")
        print(f"  LAB-ONLY 运行期父子流程共享同一 rows 对象: {record['lab_only_live_has_parent_child_alias']}")
        print(f"  LAB-ONLY 存档恢复后父子流程仍共享 rows 对象: {record['lab_only_restored_has_parent_child_alias']}")
        live_rows = _rows_text(record["live"]["public"], "vendor_a:a_park")
        b_rows = _rows_text(record["live"]["public"], "vendor_b:b_entry")
        print(f"  甲 a_park 可见 rows: {live_rows}")
        print(f"  乙 b_entry 可见 rows: {b_rows}")
        print("  结论: 仅靠该检查点的公开数据与一次序列化文件，只能证明双方看到相同的值与顺序，")
        print("        无法判定它们是否为同一嵌套对象；别名事实只存在于运行期内存。")


def report_same_order():
    _print_section("检查点二：同一完成顺序（先甲后乙）—— 返回前甲的数据")
    for case in h.CASES:
        live = s.driven_run(case, False, ("a", "b"))
        restored = s.driven_run(case, True, ("a", "b"))
        live_a = s.a_row_count_before_return(live)
        restored_a = s.a_row_count_before_return(restored)
        print(f"\n[{case}]")
        print(f"  乙编辑后、任一返回前，甲视角 A.count: live={live_a}  restored={restored_a}")
        print(f"  live     最终报表: {live['final_report']}")
        print(f"  restored 最终报表: {restored['final_report']}")
        print("  解读: 运行期乙对共享 rows 的就地改写污染了甲尚未返回的数据；")
        print("        存档恢复切断了父子别名，同样的操作次序不再产生污染。")


def report_reverse_order():
    _print_section("检查点三：反序完成（先乙后甲）—— 相同最终一行掩盖不同阶段故障")
    live = s.driven_run("no_io", False, ("b", "a"))
    restored = s.driven_run("no_io", True, ("b", "a"))
    live_a = s.a_row_count_before_return(live)
    restored_a = s.a_row_count_before_return(restored)
    print("\n[no_io] 完成顺序 b -> a")
    print(f"  返回前甲视角 A.count: live={live_a}  restored={restored_a}")
    print(f"  live     最终报表: {live['final_report']}")
    print(f"  restored 最终报表: {restored['final_report']}")
    print("  两条执行最终都只剩 [B:0]，但 live 在汇合前已发生嵌套数据串变（11->19），")
    print("  restored 保持 11；最终一行相同不能反推故障阶段或机制相同。")


def report_boundary_and_merge():
    _print_section("对照：Call Activity 返回边界与并行汇合的列表截断")
    for case, evidence in s.return_boundary_evidence().items():
        print(f"\n[{case}] 返回时 rows 是否父子同对象: {evidence['rows_object_shared_across_return']}")
        print(f"  父调用任务保留的数据键: {evidence['parent_call_keys']}")
        print(f"  非 total 变量 audit_tag 是否保留: {evidence['audit_tag_preserved']}")
    merge = s.merge_truncation_evidence()
    print("\nDeepMerge 隔离实验:")
    print(f"  两行目标并入一行输入 -> {merge['two_row_target_plus_one_row_incoming']}")
    print(f"  一行目标并入两行输入 -> {merge['one_row_target_plus_two_row_incoming']}")
    print("  => 最终少 A 的直接机械原因是 merge_array 的 del a[len(b):] 截断，")
    print("     与运行期别名污染是两个不同阶段、可独立发生的机制。")


def write_samples():
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    matrix = s.full_matrix()
    payload = json.dumps(matrix, ensure_ascii=False, indent=2, default=str)
    out = SAMPLE_DIR / "diagnosis_matrix.json"
    out.write_text(payload, encoding="utf-8")
    print(f"\n机器可读采样已写入: {out.relative_to(Path.cwd())}")


def main():
    report_midpoint()
    report_same_order()
    report_reverse_order()
    report_boundary_and_merge()
    write_samples()
    print("\n排查完成：未修改任何库代码或 BPMN 行为，仅做受控观测与存档续跑。")


if __name__ == "__main__":
    main()
