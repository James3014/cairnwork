# Cairnwork

Cairnwork 是 Nexus-new 的後繼產品：一個接任務、執行、驗證，再交給 nexus-core 公證的 agent 工作主體。三層結構（第一層：包住模型前後的輔助與完成驗證；第二層：本機模型能力；第三層：前沿模型）從這裡長出來。

Nexus-new 是**參考**，不是搬家來源。決定與理由：knowledge `nexus/decisions/2026-10-19-cairnwork-owner-decision.md`；議會條文 A–I：`nexus/decisions/council-2026-10-19-cairnwork/CONSENSUS_R2.md`。

## 狀態

試點中（2026-10-20 → 2026-11-16）。目前只接一種任務：**小 bug 修正**（eia-task 格式：allowed_files ≤10、`pytest` 驗證）。

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

- 單檔 ≤400 行、單函式 ≤80 行；核心（`src/cairnwork/`）總行數 ≤5,000 行。
- Ruff BLE001／S110 開啟；失敗情境測試證明失敗不會變成成功。
- import-linter：禁止循環、禁止核心 import `experimental/`。
- verifier sandbox 的見證測試在 macOS CI job（`test-macos`）一定會實際執行；缺少 sandbox-exec 時，這個 job 直接判失敗。

## 對外說法

README、發布說明、PR 描述只寫已經量到的結果，不寫預期的提升。11/16 試點到期時，評估標準是有沒有在 A/B 贏過裸骨幹、四種演練有沒有通過，不看功能數或 PR 數。

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
