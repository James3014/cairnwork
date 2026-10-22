# Cairnwork

Cairnwork 是 Nexus-new 的後繼產品：一個接任務、執行、驗證，再交給 nexus-core 公證的 agent 工作主體。三層結構（第一層：包住模型前後的輔助與完成驗證；第二層：本機模型能力；第三層：前沿模型）從這裡長出來。

## 定位：完成有證據

Cairnwork 是 AI 寫程式代理人的交付驗證層。核心價值不是提高成功率，而是「完成有證據」：變更唯有通過延後隱藏測試才算完成（只有 `test_source: "bundle"` 嚴格模式保證 worker 看不到測試；預設 `auto` 是相容／示範模式，不作保密主張），並可附上離線可驗證的簽署證明（見 [docs/ATTESTATION.md](docs/ATTESTATION.md)）。具體是四件事：

- **僅驗收完成**：沒通過驗收程序的候選 commit 不算完成。
- **延後隱藏測試**：驗收用的測試在 worker 結束後才套到候選上，worker 的 clone 只有 base。保密保證只在 `test_source: "bundle"`（測試只放私有存放區、沒匯入就拒絕、絕不退回 origin）成立；`auto`（程式預設）有 bundle 就用、沒有才從 origin 的公開分支抓，worker 仍可能找得到，所以不主張保密。證明包的 `test_source` 欄位記錄該次執行當時用的模式（取自執行時寫入的紀錄，不是現在的 config.json；沒有隱藏測試或舊的執行為 `none`）。
- **隔離驗證器＋Core 公證**：驗收在專用帳號與沙盒（macOS Seatbelt／Linux bubblewrap）執行，請求與結果進只增不改的紀錄，由 Core（nexus-certify）核驗並綁定確切的候選 commit。
- **簽署證明可離線驗證**：驗收紀錄可用 `cairnwork-attest statement --sign` 產出 in-toto＋DSSE Ed25519 證明包，第三方用 `cairnwork-attest verify` 只靠公鑰與本機 git 物件離線重算；PR 上可用 [cairnwork/verify-action](https://github.com/cairnwork/verify-action) 自動檢查。

不主張：

- 不主張提高模型解決任務的成功率（強模型在小 bug 上本來就很少錯）。
- 不主張取代人工程式碼審查。
- 不主張是互動式 coding agent 或對話助理。
- 不主張驗證通過就沒有缺陷：Nexus-new#2813（修正 PR #2856）是 Core VERIFIED、但獨立審查仍抓到拒絕檢查可被繞過的實例。驗證是證據，不是證明。

Nexus-new 是**參考**，不是搬家來源。決定與理由：knowledge `nexus/decisions/2026-10-19-cairnwork-owner-decision.md`；議會條文 A–I：`nexus/decisions/council-2026-10-19-cairnwork/CONSENSUS_R2.md`。

## 快速開始

```sh
uv tool install --from git+https://github.com/cairnwork/cairnwork.git cairnwork
cairnwork demo        # 五分鐘示範：隔離 clone、範圍檢查、驗證、Core VERIFIED（不需 GitHub／AI／API key）
```

完整安裝、`cairnwork init` 接真實 repo、平台註記見 [docs/QUICKSTART.md](docs/QUICKSTART.md)。

## 狀態

試點中（2026-10-20 → 2026-11-16）。目前只接一種任務：**小 bug 修正**（eia-task 格式：allowed_files ≤10、`pytest` 驗證）。

第一次 A/B（Cairnwork 過濾 vs 裸跑，28 題）：**打平、未贏**，原因與下一步見 [docs/results/2026-10-22-ab-run2.md](docs/results/2026-10-22-ab-run2.md)。
延後驗收擋錯能力量測（偵測器評估、方向性）：真實模型錯誤只有 7 份、擋下 5，不下結論；規則式突變 205/205 擋下；真實正確修補誤擋 0/83；驗證通過不等於沒有缺陷。見 [docs/results/2026-10-22-filter-mechanism.md](docs/results/2026-10-22-filter-mechanism.md)。

## 能力入場規則

| 類別 | 例子 | 放哪裡 | 怎麼進來 |
|---|---|---|---|
| 必要保障 | 授權、隔離、重送去重、取消、重啟恢復、只有 Core VERIFIED 才算完成 | `src/cairnwork/` | 契約測試＋失敗情境測試＋恢復測試 |
| 實驗能力 | xray、codeintel、pregate、memory、belief、learning | `experimental/`，預設關閉 | 具名負責人；14 天到期（自「裸骨幹＋自有 A/B 基準就緒」起算），到期未證明就刪 |
| 正式能力 | 實驗能力升格 | `src/cairnwork/` | 在 Cairnwork 自己的 A/B 贏過裸骨幹（同任務、同模型、同評分器，事先登記門檻） |

- 舊版在 Nexus-new 有效，不算 Cairnwork 版有效的證據；沒有證據＝未證實，不是無用。
- 裸骨幹通過正常、失敗、重啟、回退四種演練之前，不搬任何實驗能力。
- 核心程式不得 import `experimental/`。

## 邊界

- 第一天的依賴只有 `nexus-core`（nexus-certify）和 `nexus-runtime`，以固定版本引用。
- 工具類 sibling repo（RIE、opencli-reviewer、open-swe-runtime、workbench、repo-learning）只透過版本化的輸入輸出契約呼叫。nexus-deployment-lab 是部署控制端，不是下層工具。devspace 不接。
- sibling repo 永遠不得 import Cairnwork。
- Cairnwork 不得改動 Nexus-new#2721 的量測環境。

## 硬性限制（CI 強制）

- 單檔 ≤400 行、單函式 ≤80 行；獨立套件 `cairnwork_sandbox` 上限 1,200。
- 核心（`src/cairnwork/`）5,000 行是警戒預算，不是硬上限：超過時 CI 發 WARNING、不失敗，但 PR 必須簡短交代原因、替代方案（為何不用既有實作／契約／adapter）、責任（沒有第二權威或重複狀態）、成本與驗證。不要為了避開計數器新建 `cairnwork_*` 套件，程式放哪裡看責任。
- Ruff BLE001／S110 開啟；失敗情境測試證明失敗不會變成成功。
- import-linter：禁止循環、禁止核心 import `experimental/`。
- verifier sandbox 的見證測試在 macOS CI job（`test-macos`）一定會實際執行；缺少 sandbox-exec 時，這個 job 直接判失敗。

## 對外說法

README、發布說明、PR 描述只寫已經量到的結果，不寫預期的提升；主張一律照「完成有證據」定位（交付有程序證據），不講提高模型成功率。11/16 試點到期時，評估標準是有沒有在 A/B 贏過裸骨幹、四種演練有沒有通過，不看功能數或 PR 數。

## 參與方式

要改 Cairnwork，先寄 coord 信給整合負責人（`coord who repo:cairnwork`）取得範圍。同時開發的 Claude session 最多 2 個。

## 文件

- `docs/PRESERVED_BEHAVIORS.md`：試點必須保留的必要保障（議會 D 條）。
- `SECURITY.md`：事故應變。
- `docs/LESSONS_FROM_NEXUS.md`：Nexus-new 犯過的錯與 Cairnwork 的對應規則（開工前必讀）。
- `docs/DRILLS.md`：四種演練的實際結果。

## 開發

```
uv sync
uv run ruff check .
uv run lint-imports
uv run python scripts/check_size.py
uv run pytest
```

## License

Apache License 2.0 — see [LICENSE](LICENSE). Copyright 2026 James3014.
