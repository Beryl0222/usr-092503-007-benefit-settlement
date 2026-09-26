"""案件身份：以业务事实生成唯一案件。

自然键 = (参保人, 参保关系, 就医地, 分娩日期)。
医院重试、经办人补正、异地回执乱序都携带同一自然键，归并到同一案件，
与传输通道、报文顺序无关。
"""

import hashlib


def case_natural_key(person_id: str, enrollment_id: str, care_region: str,
                     delivery_date: str) -> str:
    """由业务字段计算案件自然键（大小写不敏感、去空白）。"""

    parts = [
        str(person_id).strip().lower(),
        str(enrollment_id).strip().lower(),
        str(care_region).strip().lower(),
        str(delivery_date).strip(),
    ]
    if any(not p for p in parts):
        raise ValueError("案件自然键的每个字段都不能为空")
    raw = "|".join(parts)
    return "CASE-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16].upper()
