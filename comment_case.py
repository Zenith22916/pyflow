# -*- coding: utf-8 -*-
"""注释渲染验证：复合语句头注释、嵌套缩进、多行注释组。"""


def check(items):
    # 函数起始说明
    i = 0
    # 循环开始前检查（挂 while 头）
    while i < len(items):
        # 嵌套层级一（挂 if 头）
        if items[i] < 0:
            # 嵌套层级二（挂叶子语句）
            # 组内第二行
            i += 1
            continue
        i += 1
    return i
