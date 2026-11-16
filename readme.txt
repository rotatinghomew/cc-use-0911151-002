人工智能开放场景需求撮合与试点治理平台

试点基础容量：Application.register_supplier 登记供应商与当前可占用的并行试点名额，
accept_pilot 将已登记需求占用一个名额；finish_pilot 记录完成或退出，释放当前名额，
历史试点结果保留。capacity_status 可查询余额及已接受试点。事务内检查余额并接受试点，
避免两个试点竞争一个席位时超额确认。

试点效果复盘：record_selection_batch 在入选时保存入选前需求快照，并同时登记未入选需求作为
对照；该快照不可修改，复核使用当时的条件而不是今天调整后的需求。record_observation_batch
按月补报观察结果，支持首次上报、补报、更正和撤回；未到观察期的入选需求计为待观察，不按失败
处理，观察期已到但未补报的计为数据缺口。effect_review 只在同行业、入选前评分接近的范围比较
入选与未入选需求的启动率、退出率和平均收益，无法比较的行业单独给出原因。未到观察期的项目
不进入比较，也不影响续期意见。

续期待审意见：refresh_renewal_opinion 依据当前观察批次生成意见，并记录算法、规则、入选批次、
观察批次和每条观察记录的指纹。补报、撤回或试点退出只重评尚未审议的意见；已经作出的续期决定
保留作出时的依据摘要，不随新月报改变。reproduce_renewal_decision 用决定记录中的批次核对审批
时到底用了哪批观察结果。

补报与复盘验收：
python3 -m scene_exchange record-selections --database /tmp/review.sqlite3 --round round-2026a --batch selection-2026a --fixture fixtures/effect_review.json
python3 -m scene_exchange record-observations --database /tmp/review.sqlite3 --round round-2026a --batch observation-2026-02 --period 2026-02 --kind initial --fixture fixtures/effect_review.json --recorded-at 2026-02-03T10:00:00+08:00
python3 -m scene_exchange record-observations --database /tmp/review.sqlite3 --round round-2026a --batch observation-2026-03-supplemental --period 2026-03 --kind supplemental --fixture fixtures/effect_review.json --recorded-at 2026-03-05T10:00:00+08:00
python3 -m scene_exchange review-pilots --database /tmp/review.sqlite3 --round round-2026a --refresh
python3 -m scene_exchange pending-opinions --database /tmp/review.sqlite3 --round round-2026a
python3 -m scene_exchange decide-renewal --database /tmp/review.sqlite3 --round round-2026a --opinion <opinion_id> --outcome conditional_renew --approver 主管部门
python3 -m scene_exchange reproduce-decision --database /tmp/review.sqlite3 --decision <decision_id>

运行验收：
python3 -m scene_exchange run --fixture fixtures/acceptance.json --database /tmp/scene_exchange.sqlite3 --output /tmp/scene_exchange-output

核验：
python3 -m scene_exchange verify --database /tmp/scene_exchange.sqlite3 --output /tmp/scene_exchange-output

全量测试：
python3 -m unittest discover -s tests -v

按模块测试：
python3 -m unittest tests.intake.test_demand_contract -v
python3 -m unittest tests.pilots.test_retrospective -v

构建检查：
python3 -m compileall -q scene_exchange tests
