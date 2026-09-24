# -*- coding: utf-8 -*-
"""压力测试：多行语句、嵌套复合结构、with、嵌套定义。"""


def fetch_rows(limit=10):
    """模拟取数：多行函数调用与列表定义。"""
    rows = fetch(
        "select id, name, amount "
        "from orders",
        limit=limit,
        timeout=30,
    )
    data = [
        ("a", 1),
        ("b", 2),
    ]
    return rows, data


def summarize(items):
    total = 0
    skipped = 0
    for name, value in items:
        if value is None:
            skipped += 1
            continue
        if value < 0:
            # 负数直接终止统计
            break
        total += value
    else:
        print("循环自然结束")
    return total, skipped


def save_snapshot(payload):
    """with 语句与嵌套定义演示。"""
    def encode(obj):
        # 简单编码函数
        return str(obj)

    with open("snap.txt", "w", encoding="utf-8") as fh:
        fh.write(encode(payload))
    return encode


def run_pipeline():
    try:
        rows, data = fetch_rows(limit=5)
        total, skipped = summarize(data)
        if total > 10:
            print("总量偏大", total)
            if skipped > 0:
                print("有跳过项:", skipped)
        else:
            print("总量正常")
        save_snapshot({"total": total})
    except OSError:
        print("文件写入失败")
    print("管线结束")


if __name__ == "__main__":
    run_pipeline()
