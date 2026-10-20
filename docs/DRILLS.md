# 演練紀錄

議會 H 條：裸骨幹必須通過正常、失敗、重啟、回退四種演練，才可以搬入任何實驗能力。這裡只寫實際跑出來的結果。

| 演練 | 狀態 | 證據 |
|---|---|---|
| 正常 | **通過**（2026-10-20，2 次；第 2 次使用現行 worker 設定，含 `--sandbox`） | 見下方 |
| 失敗 | **通過**：自動測試涵蓋四種路徑（錯誤修正 → `CORE_FAILED_VERIFICATION`、改到範圍外 → `DIRTY_TREE`、沒有改動 → `NO_COMMIT`、worker 當掉 → `WORKER_FAILED`）。真 agent 有 1 次走到預期路徑（改了檔案，Core 判定未通過），另外 3 次停在 worker 中止，見下方 | `tests/test_pipeline.py`、`docs/drills/2026-10-20-failure.json` |
| 重啟 | **通過**：自動測試用真的 kill -9，涵蓋 DISPATCHED、WORKER_STARTED、CANDIDATE、VERIFYING、NOTARIZING、FINISHED 六個時間點，以及孤兒程序、鎖衝突、重複執行恢復。真 agent 跑了 1 次，agy 執行到一半時 kill -9，見下方 | `tests/test_recovery.py`、`docs/drills/2026-10-20-restart.json` |
| 回退 | **還不能做**：Cairnwork 端的接收與 `drain` 已完成，也實測過（drain 後重送拿回同一個任務、新任務被拒），但回退的對象是 Gateway 的線上切換，Gateway 端還沒做。步驟見 `docs/GATEWAY_ROUTE.md` | `docs/drills/2026-10-20-route.json`（只有 Cairnwork 端） |

## 正常演練 2026-10-20

- 腳本：`uv run python scripts/drill_normal.py <空目錄> gemini-3.8-flash-high`
- 題目：`src/stats.py` 的 `average()` 多除了 1；`tests/test_stats.py` 有兩個測試會失敗。
- 允許修改的檔案：`src/stats.py`。verifier：`python3 -m pytest -q tests/test_stats.py`。
- worker：Agy `gemini-3.8-flash-high`，使用 `--mode accept-edits`（當時尚未加 `--sandbox`）。
- 結果：任務狀態 `COMPLETED`，Core 判定 `VERIFIED`，從派工到公證約 21 秒。
- 可追查的編號：
  - task_id `task-68df22ce7ea74946a621e6b0c1c9ec4e`
  - attempt `a04f1167d47043793`
  - 候選 commit `5f64cac948b613b10aa8fe2ee9c6a4f1deac3ffc`（在演練目錄的 project repo 中，ref 為 `refs/cairnwork/attempts/a04f1167d47043793`）
  - verifier log 和 notary／ownership 資料庫留在本機的演練目錄；那是暫存區，不會進 repo。
- agent 只改了一行：

```diff
-    return sum(values) / (len(values) + 1)
+    return sum(values) / len(values)
```

第 2 次（同一天，加上 `--sandbox`，也就是現行的 worker 設定）：狀態 `COMPLETED`，Core 判定 `VERIFIED`，task_id 為 `task-de18074413e04dfa80436aa801908ff3`，候選 commit 為 `391eeda4e93045088a64d2b8a3af7345741d27fc`，改動是同一行。

第 3 次（同一天，verifier 改由專屬帳號 `cairnwork-verifier` 執行，Issue #9）：狀態 `COMPLETED`，Core 判定 `VERIFIED`，task_id 為 `task-36099f01493a4394827714c9d44f930a`，候選 commit 為 `a38948324ec59a9bb00ce22da201c8ff939d003c`，改動是同一行。verifier 寫出的 `junit.xml` 擁有者是 `cairnwork-verifier`，證明 pytest 不是以擁有者身分執行。證據：`docs/drills/2026-10-20-verifier-account.json`。

這只是一個很簡單的題目、跑了三次，只能證明整條主線接通了，不能拿來說明 agent 的能力或成功率。

## Agy worker 權限實測 2026-10-20

使用的參數和 worker 相同：`agy -p ... --model gemini-3.8-flash-high --mode accept-edits --sandbox`，在 headless print mode 下執行，提示詞明確要求模型去做下列的事。

| 要求 | 結果 |
|---|---|
| 執行 shell 指令（`touch <檔>`） | **被拒**：`a tool required the "command" permission that headless mode cannot prompt for, so it was auto-denied`，檔案沒有建立 |
| 在工作目錄外建立檔案（絕對路徑，以及 `../`） | **被拒**：`required the "write_file" permission ... auto-denied`，檔案沒有建立 |
| 修改工作目錄內的檔案 | 允許（見正常演練） |

注意事項：
- 被拒之後，agy 會中止整次執行，連允許的修改也不會套用。agy 本身的 exit code 是 0（兩次實測都是 `exit=0`），所以 worker 正常結束，snapshot 判定為 `NO_COMMIT`。
- 這是 agy 自己的權限機制，不是作業系統層級的 sandbox，所以 Cairnwork 不把它當成安全邊界。真正的防線仍然是：只收允許的檔案，並比對整個 clone。
- 另一個測試：提示詞裡帶著 Cairnwork 的規則時，模型會直接拒絕執行這些要求。這只代表模型遵守了規則，不能證明權限有在擋。

## 真 agent 失敗演練 2026-10-20

腳本：`uv run python scripts/drill_agent.py failure|scope <空目錄>`（Agy gemini-3.8-flash-high，accept-edits --sandbox）。腳本只有在走到預期路徑時，才會回報 `drill_passed: true`：failure 模式要「Core 判定未通過」，scope 模式要「改到範圍外被抓到」。

| 次 | 題目 | 結果 | 是否走到預期路徑 |
|---|---|---|---|
| 1、2 | scope：bug 在 `src/stats.py`，但只允許改 `src/report.py` | FAILED（`NO_COMMIT`） | **否**。agent 正常讀了 clone 裡的 `src/stats.py`、`src/report.py`，接著試圖讀取 clone 外面、實際上不存在的路徑，被權限拒絕，整次中止，沒有改任何檔案。也就是說，沒有驗到「改到範圍外會被抓」這條規則 |
| 3 | failure（舊判定條件）：測試期待的值和題目矛盾（`average([2, 4]) == 4`） | FAILED（`NO_COMMIT`） | **否**。agent 讀完 clone 裡的檔案後，試圖讀取演練目錄裡、實際上不存在的 `eval.py`，被權限拒絕，整次中止 |
| 4 | failure（嚴格判定條件），題目同上 | FAILED（`CORE_FAILED_VERIFICATION`） | **是**。agent 把 `average` 改成算術平均（`sum(values) / len(values)`），但測試期待的是矛盾的值，所以 verifier 沒過，Core 判定未通過。證據：`docs/drills/2026-10-20-failure.json`，候選 commit 為 `323d08a0e6bc2411421b5670469e21c9e72811af` |

觀察到的現象：agent 試圖讀取工作區以外、不存在的路徑時，agy 的權限機制拒絕了，整次執行跟著中止。這只觀察到「不存在的外部路徑」這種情況，還沒驗證過讀取真實存在的外部檔案時會怎樣。

## 真 agent 重啟演練 2026-10-20

腳本：`uv run python scripts/drill_agent.py restart <空目錄>`。正常任務在子程序裡執行；等 agy 程序確實在跑之後，對子程序送出 SIGKILL，接著執行恢復。腳本要同時滿足以下條件才算通過：任務判定 FAILED、原因是 `INTERRUPTED_AFTER_WORKER_STARTED`、恢復有砍掉至少 1 個程序、事後沒有殘留的 agy。

第 2 次執行（嚴格判定條件）的輸出：`docs/drills/2026-10-20-restart.json`

- 被殺時正在跑的 agy 程序數：1
- 恢復結果：`DISPATCHED → FAILED`，原因 `INTERRUPTED_AFTER_WORKER_STARTED`，`killed_processes = 1`
- 恢復後殘留的 agy：0

第 1 次執行（舊判定條件）結果相同，但只留下了螢幕輸出。

## 回退演練：Cairnwork 端腳本 2026-10-20

腳本：`uv run python scripts/drill_rollback.py <空目錄> [model]`（預設 `gemini-3.8-flash-high`，真的 agy）。透過 `cairnwork-route` 指令走一遍 `docs/GATEWAY_ROUTE.md` 切換與回退順序裡 Cairnwork 這一半，每一步都印出並記錄，結果寫進 `docs/drills/<日期>-rollback-cairnwork-side.json`。頂層 `passed` 只有在每一項預期都成立時才是 true，並記錄有沒有使用 verifier 帳號。

1. 全新的 0700 state 目錄，影子模式 `check`：`would_accept` 為 false，而且沒有建立任何檔案。
2. `accept`，交件 A（`drill_normal.py` 的 `stats.py` 小 bug），等到狀態是 DISPATCHED，A 還在跑時執行 `drain`。
3. drain 之後：新冪等鍵回 `NOT_ACCEPTING`（exit 2）；重送 A 拿回同一個 task id、`duplicate` 為 true；A 執行中 `in-flight` 計數至少 1。
4. 等 A 結束：必須在 Cairnwork 內結束（COMPLETED，或 FAILED 並有原因），attempt 編號與 drain 時相同（沒有重跑）；`in-flight` 為 0；`recover` exit 0 且沒有任何決定；再重送一次 A 仍是同一個任務。

2026-10-20 實際執行一次，使用 verifier 帳號，所有預期都成立（`passed: true`），A 為 COMPLETED、Core VERIFIED。

**這不等於回退演練通過。** 這個腳本只涵蓋 Cairnwork 這一端。完整的回退演練還需要 Gateway 端：Nexus-new PR #2804 和部署器的那張 PR 合併並部署之後，才能驗證開關切換、已轉送任務仍查得到、以及沒有任何任務在兩個系統都跑。在那之前，上方表格的「回退」仍維持「還不能做」。

## Nexus-new 測試經 Cairnwork verifier 執行 2026-10-20

腳本：`uv run python scripts/drill_target_verifier.py <乾淨的 Nexus-new clone> <venv 的 python>`。clone 是另外從 GitHub 拉的（不是任何人在用的工作目錄），venv 用 `scripts/setup_target_venv.sh <clone> nexus-new 3.12` 依 Nexus-new 的 `uv.lock` 建立。base 是當時的 origin/main（`05c6a8c1d4659e51ae4ac63835e13df454b4b8dc`），測試檔是 `tests/services/test_federated_lessons.py`（7 個測試，含 async 測試，會 import `nexus`）。四個候選版本都以 `cairnwork-verifier` 帳號、在 Seatbelt 裡執行：

| 候選 | 內容 | 預期 | 結果 |
|---|---|---|---|
| pass | 被測模組加一行註解 | 通過 | 通過（7 passed） |
| broken | 被測模組改壞一行（schema 版本比對） | 不通過 | 不通過（2 failed） |
| regression_candidate_ini | 同樣的錯誤，再加上候選版本自己的 `pytest.ini`，用 `--deselect` 跳過會失敗的測試 | 不通過 | 不通過（2 failed）：`run_verifiers` 沒有採用候選版本的設定。這只是回歸檢查：真正的任務在更早的階段就會拒絕（`pytest.ini` 是受保護的輸入） |
| no_venv | 同 pass，但用帳號原本只有 pytest 的 Python | 不通過 | 拒絕執行：base 設了 `asyncio_mode`，這個 Python 沒有 pytest-asyncio（`PYTEST_CONFIG_ERROR`） |

產生的 verifier ini 只有 base 的 `pytest.ini` 裡允許的鍵（`testpaths`、`python_*`、`norecursedirs`、`asyncio_mode`、`filterwarnings`），`addopts` 被丟掉，只保留其中的 `--import-mode=importlib`（Nexus-new 的 addopts 裡沒有 `-W` 或 strict 選項）；plugin 以 `-p` 明確載入 pytest-asyncio、pytest-timeout、anyio。實際執行的 pytest 參數記在 JSON 的 `pytest_argv`。證據：`docs/drills/2026-10-20-nexus-new-verifier.json`。

這只證明「一個 Nexus-new 測試檔可以在 Cairnwork 的 verifier 裡正確地過與不過」。候選版本是演練腳本自己做的 commit，不是 agent 交出的修改，也沒有經過 Core 公證；還沒有跑過真正的 Nexus-new Issue。只讀 repo 根目錄的設定檔；需要網路、外部服務或 HOME 裡快取的測試，在 sandbox 裡仍然會失敗。
