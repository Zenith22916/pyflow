# -*- coding: utf-8 -*-
"""流程图生成器的演示模块：覆盖常见语句形态的测试用例。"""

import math


def calculate(x, y, op="add"):
    """四则运算：按 op 计算并返回结果。"""
    # 目前只支持加法和乘法
    if op == "add":
        result = x + y
    elif op == "mul":
        result = x * y
    else:
        # 未知运算符返回 0
        result = 0
    return result


def validate(items):
    """校验列表：全部元素有效返回 True，遇到空值提前返回 False。"""
    for item in items:
        if item is None:  # 空值直接判定失败
            return False
        if isinstance(item, str) and len(item) > 100:
            return False
    return True


def process_data(a, b):
    """数据处理主流程：比较、累加、异常兜底。"""
    if a > b:
        diff = calculate(a, b)
        print("a 更大", diff)
    elif a == b:
        print("相等")
    else:
        diff = calculate(b, a, op="mul")
        print("b 更大", diff)

    # 循环累加演示
    total = 0
    i = 0
    while i < 3:
        total += i
        i += 1

    for k in range(2):
        print("k =", k)

    try:
        value = int("42")
        print("解析成功:", value)
    except ValueError:
        # 转换失败走这里
        print("转换失败")
    finally:
        print("try 块结束")

    return total


def format_price(amount):
    """金额格式化：嵌套判断演示。"""
    if amount < 0:
        return "无效金额"
    if amount < 100:
        return f"{amount} 元"
    if amount < 10000:
        return f"{amount / 1000:.1f} 千元"
    return f"{amount / 10000:.2f} 万元"


def report(a, b):
    ok = validate([a, b])
    if not ok:
        print("校验未通过")
        return
    total = process_data(a, b)
    text = format_price(total)
    print("报告:", text)
    print("平方根:", math.sqrt(total))


def main():
    ok = validate([1, 2, 3])
    if ok:
        result = process_data(5, 3)
        print("结果:", result)
    else:
        print("初始校验未通过")


def standalone():
    # 独立入口：没有被任何函数调用
    return calculate(1, 2)


if __name__ == "__main__":
    main()
    print(standalone())
