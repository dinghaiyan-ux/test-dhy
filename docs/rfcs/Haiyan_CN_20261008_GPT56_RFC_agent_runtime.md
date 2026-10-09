# RFC：Enterprise Agent Engine（EAE）生产级多租户 Agent 运行时

**状态：草案 / 待同行评审，不代表已批准或已启动工程实施。** 2026-10-09。作者：Haiyan 发起的技术方案整理。目标仓库和 Jira 项目尚待指定；本地原稿不会自动公开。基线：`Haiyan_CN_20261008_GPT56_agent_runtime_whitepaper.md` 与 `Haiyan_CN_20261008_GPT56_eae_architecture_spec.md`。前者为证据和架构选择研究，后者为详细 OpenAPI/Protobuf、Schema、状态机与 Python 原型的唯一技术规范；本文不声称有统一实测性能或已获安全认证。

## 1. 决策请求及目标

请求原则性批准 M0–M1 的技术验证与威胁建模，**不是批准立即跨云上线或购买 GPU**。企业需要在授权范围内运行不可信 Agent 工具和长程会话，在不同租户/区域间隔离代码、数据、设备与外部 SaaS 权限，同时量化每个有效任务的尾延迟、错误率和总成本。M2 高可用、M3 GPU 池、M4 跨云路由分别单独过闸；未过安全和数据驻留评审则排队/拒绝。暂不承诺集群间活体进程/GPU 热迁移。

## 2. 背景、问题与证据

现有简单容器共享宿主内核，不能单凭 namespace/cgroup 作为敌对多租户的完整安全证明；也不能将 microVM 启动速度作为用户首个有效结果的端到端时延。gVisor 有用户态 syscall 中介及 Gofer，Kata/Firecracker 有客户内核边界，Wasm 有显式导入能力；GPU 驱动穿透是另一信任边界。研究白皮书 §1–8 详述差异和一手来源 [1][3][4][5][6][8][15][16][17][18]。旧基准与厂商案例不能跨配置排名。所有性能门槛须在目标软硬件上独立压测。

## 3. 提案架构与职责

```text
客户/Agent SDK → Gateway/WAF → IAM/PDP/Quota → Session/Task API
                                          ↓ 事务状态 + outbox
                                  全局 Scheduler / 公平队列
                           ┌──────────────┴──────────────┐
                 Placement Adapter                 Sandbox Controller
                 地域/集群策略                      生命周期/fencing
                           └─────── 各区域 CPU/GPU 池 ─────┘
                          Wasm | gVisor | Kata/VM | 专属 GPU/MIG*
       区域权威状态数据库（quorum）+ 对象检查点 + 可失缓存 + Tool Gateway
       *须以目标硬件、VMM、驱动兼容矩阵/隔离审计验收
```

Gateway 严格验证身份及幂等；PDP 判动作、地域、数据分级，Quota 原子预留；Scheduler 先硬过滤合规池再打分；Placement Adapter 跨集群路由，Kueue 可用于 batch admission，Karmada 可用于多集群放置，**一个任务只能有一个最终放置 owner**；Controller 分配沙箱、检查签名/节点健康、续期/销毁；状态层以资源版本和 lease epoch 控制单会话唯一 owner；Tool Gateway 对外部动作再次核对用户授权目标和内容。完整拓扑、数据流见架构规范 §1–3。

## 4. 核心权衡与备选

| 路线 | 收益 | 代价/结论 |
|---|---|---|
| 继续单一普通 OCI 容器 | 上线最快、运维简单 | 共享宿主内核；敌对租户不能仅靠此方案，拒绝作为默认强隔离边界 |
| 全部 VM | 强边界、Linux 兼容 | 预热/客户内核/运维成本高；高密度小插件没必要全走 VM |
| 单一 gVisor | Linux 工具路径可复用 | syscall/Gofer 与设备 ioctl、ABI 限制；高风险任务需独立 VM/设备池 |
| 全部 Wasm | 细粒度能力、密度潜力 | 原生 Linux CLI、浏览器与 CUDA 适配难；仅适合能重构负载 |
| 本提案分层多运行时 | 风险分层、数据驻留/设备独立审核 | 多运行时运营复杂；必须做兼容矩阵、统一策略与 observability |

## 5. 关键接口、状态和验收准则

外部 API `POST /v1/sessions`、`POST /v1/sessions/{id}/steps`、`POST ...:pause/:resume`、`GET /v1/sessions/{id}`、`GET /v1/sessions/{id}/events`，显式幂等键、`If-Match` 与租户 scope；内部 RPC `Reserve/Heartbeat/Release/Start/CommitStep`，要求调用者 mTLS 与服务端二次鉴权。状态流覆盖 QUEUED、PLACED、READY、RUNNING、CHECKPOINTING、PAUSED、RESUMING、终态。对象先写后 CAS 提交 manifest，旧 epoch 拒绝写状态和调用外部工具；outbox 解决事务事件交接而不是承诺对外部 SaaS exactly-once。详见规范 §4–7。

**必须通过**：两个调度实例争夺最后一个 GPU slot 不超卖；网络分区时旧 worker 在 Tool Gateway 和存储侧均被栅栏拦截；AZ 故障已提交检查点不丢，quorum 丢失 fail-closed；缓存失效能按 manifest 恢复；未经批准地域不可被自动溢出；GPU/驱动/镜像不在验证矩阵时隔离节点；对不支持幂等的第三方 API 保留 `UNKNOWN` 和人工对账路径。接口兼容、审计、SAST/SBOM、灰度回滚均为上线门槛。

## 6. 演进路线与 WBS（工作量为评审基线，不是已确认排期）

估算单位为**人日（PD）**，1 PD 暂按 8 小时；含研发/测试/安全评审的初步工作量，不含采购、云资源、法务认证和 30% 风险储备；多人并行会影响日历周期但不减少人日。里程碑为相对批准日 T0 的暂定窗口，具体日历日期须项目负责人、假期/资源及用户确认后才能写入 Jira。

| 里程碑 | WBS / 拆解（括号为估算 PD） | 依赖与退出标准 |
|---|---|---|
| M0，T0–T0+2 周 | 需求/负载矩阵(5)；威胁建模与合规驻留(8)；SLO/成本基线(5)；接口和数据模型评审(6) | 24 PD；跨安全、平台、财务签核；不得以本 RFC 代替授权 |
| M1，T0+3–8 周 | Gateway/IAM/PDP(12)；Quota Ledger(10)；Session/Task API(12)；Scheduler 单区域(15)；Sandbox Controller+CPU adapters(18)；Checkpoint/Context(12)；Tool Gateway(12)；合同/并发/安全集成测试(16) | 107 PD；依 M0；至少单区域 CPU 端到端闭环和上述验收 |
| M2，T0+9–12 周 | Raft/状态库 HA 与故障演练(12)；outbox/回放/审计(9)；网络 VPC/eBPF 策略(10)；预热弹性/熔断(9)；DR 与分区测试(10) | 50 PD；依 M1；在 AZ 故障及 quorum 故障分别验证故障语义 |
| M3，T0+13–16 周 | GPU 设备/驱动兼容矩阵(8)；专属 GPU 池及 MIG* 隔离(13)；设备清理/侧信道/驱动安全测试(12)；模型及显存成本压测(8) | 41 PD；依 M2；*仅经硬件支持和租户隔离验收后启用 |
| M4，T0+17–22 周 | Kueue/Karmada 职责与适配实验(10)；跨云策略/数据驻留/备份(12)；成本路由与财务核算(8)；混沌、审计、金丝雀(14) | 44 PD；依 M2 与经批准的目标云；不得以跨云路由自动推断 GPU 热迁移 |

**合计基线 266 PD = 2,128 小时**；另提议 30% 风险储备约 80 PD（按整人日取整），预算测算按上限 **346 PD / 2,768 小时**。这是比较与评审用模型，非承诺。M3/M4 可并行验证但不绕过 M2 安全门槛；M0 不意味着立刻在 Jira 建立既定日历里程碑。

## 7. 风险登记、评审问题和升级闸口

1. **跨云 split-brain 与外部副作用重复**：谁拥有 home region 的最终写权？Tool Gateway 如何拒绝旧 epoch？不支持幂等的 SaaS 调用怎样人工对账？
2. **GPU 共享是否改变威胁模型**：NV 驱动与 nvproxy 审计责任？目标型号/MIG profile/驱动 ABI 是否受支持？遇故障谁负责设备归零？
3. **控制面与调度器风暴**：冷启动 P95/P99 按哪个请求分段统计？预热成本如何入账？什么时候压制自动扩容？
4. **数据驻留和合规**：跨云副本、备份、日志和向量缓存的物理地域如何审计？未批准地域是否完全不会被选择？
5. **隔离策略本身退化**：VPC/eBPF/CNI/宿主特权路径如何叠加防御？PDP 或 KMS 故障时 fail-closed 范围？
6. **成本与工程机会成本**：266 PD 的基础估算谁复核？基线任务单价与云成本是否有可信样本？何时裁撤复杂度不足以支撑的 runtime？

**需要评审者明确回复**：批准/反对 M0、分层 runtime 的强隔离等级、单一 home-region 写者策略、Kueue/Karmada 所有权、首批目标负载与禁止纳入的负载；给出需要补测的场景。未确认 reviewer 身份前，不指定具体人名或声称已征求 CTO 批准。

## 8. 交付边界与撤回策略

本地 RFC 与架构规范可用于评审；**不可因为仓库名称含 test 就认为可以公开内部材料**。发布前确认仓库 `owner/repo`、可见性、RFC 内容的披露许可。PR 最好在独立 `rfc/eae-agent-runtime` 分支提交，再以 PR 发起评审且不要自动 merge；若仓库不可用，可在用户明确选定的测试仓库发 Issue 并链接本地版本摘要，避免泄露私密白皮书全文。若测试发现关键隔离/身份边界不满足，停止发布生产流量、冻结新沙箱、从已提交检查点恢复并回滚 runtime adapter；任何已发出的外部副作用需单独核账，不能靠回滚文件撤销。
