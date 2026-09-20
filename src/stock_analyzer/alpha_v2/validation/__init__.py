"""Alpha V2 Validation 包（M3：Production Shadow Validation & Clean OOS）。

与 M1（正确性地基）/ M2（研究与 Shadow 能力层）的关系：

- **M2 交付的是能力**：双轨、台账、多 Head、成熟函数；
- **M3 交付的是"身份与纪律"**：冻结清单（``freeze``）、epoch 账本
  （``epoch``）、T 日预测快照（``shadow_capture``）、未来成熟回填
  （``outcome_maturation``）、M3 KPI 汇总（``validation_kpis``）、
  冻结模型工件（``frozen_model``）、DF-M2-003 数据诊断
  （``feature_diagnosis``）。

铁律（M3 §4/§7/§13，全部以测试 + 结构守卫形式落地，不靠自觉）：

- 一个时刻只存在一个 open epoch；冻结对象有变 = 关旧开新；
- T 日快照只在 T 日写入，事后重写同键必须逐字段一致，否则抛错
  （``ShadowTamperError``）；
- 快照缺失只能记 ``missing_prediction_day``，不得事后"补一个好看的结果"
  混进 clean OOS；
- 概率字段没有 OOS 校准就不写概率（写 ``not_available``）。
"""
