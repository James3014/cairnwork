# 舊 Gateway 轉送一類任務給 Cairnwork（議會 F 條）

這份是 Cairnwork 這一端的介面，以及舊 Gateway（Nexus-new）那一端需要配合的改動。Gateway 端的寫法以 Nexus-new `b488218`（2026-10-20 線上版本）為準；行號引用前要再核對。

## Cairnwork 這一端（已完成）

Gateway 保留自己的入口和認證，只呼叫一個指令 `cairnwork-route`。每個指令在 stdout 輸出一個 JSON 物件。

| 指令 | 用途 | 成功時輸出 |
|---|---|---|
| `check` < request.json | 影子模式：只檢查請求，不綁定、不寫檔、不執行 | `{"would_accept": bool}` |
| `submit` < request.json | 綁定並開始執行；同一個冪等鍵重送時，回傳同一筆任務，不會重跑 | `{"task_id", "status", "duplicate"}` |
| `status --task-id ID` 或 `--key 冪等鍵` | 查詢 | `{"task_id", "idempotency_key", "owner", "status", "attempt_id", "phase", "detail", "candidate_commit", "core_verified"}` |
| `cancel --task-id ID` | 只能取消還沒開始的任務；已經在跑的會跑完 | `{"task_id", "status": "CANCELLED"}` |
| `in-flight` | 給部署器的靜止閘門用 | `{"accepting", "count", "task_ids"}` |
| `accept`／`drain` | 打開／關閉接新任務（**預設是關閉**） | `{"accepting": bool}` |
| `recover` | 重開機後執行，處理中斷的任務，不會重跑；有執行程序還在跑時會拒絕 | `{"decisions": [...]}` |

所有指令都要加 `--state <目錄>`。目錄只有擁有者能讀寫，存放 ledger、公證、journal、請求、執行目錄和 `config.json`（`model`、`agy`、`verifier_account`，預設使用專屬 verifier 帳號）。

**Exit code**：0 成功；2 拒絕（`{"error", "detail"}`，例如 `NOT_ACCEPTING`、`BAD_REQUEST`、`BAD_SCOPE`、`NOT_CANCELLABLE`、`RUN_LOCK_BUSY`、`KeyReusedWithDifferentRequest`）；3 查不到這個任務（`UNKNOWN_TASK`）；1 其他錯誤。**1 和 3 一律當作「不知道」**，不可以當作失敗而改派給別的系統。

**請求格式**（封閉格式，多一個欄位或少一個欄位都會拒絕）：

```json
{
  "idempotency_key": "<Gateway 的 task_id>",
  "card_hash": "<task_card_hash>",
  "title": "...", "instructions": "...",
  "repo": "/絕對路徑", "base_commit": "<40 位 hex>",
  "allowed_files": ["src/x.py"],
  "verifiers": [["pytest", ["python3", "-m", "pytest", "-q", "tests/test_x.py"]]]
}
```

**保證**：

- 綁定（冪等鍵、請求摘要、所屬系統 `cairnwork`、task_id）是一筆原子的 SQLite 交易，而且在開始執行之前完成。
- 交件時持有 run lock，再把它交給背景執行程序，所以從綁定到執行結束，復原都不能插手。執行程序死掉時，kernel 會放掉鎖，之後由 `recover` 判定 FAILED，不會重跑。
- 只有 Core 對這個候選 commit 判定 VERIFIED，任務才算 COMPLETED。

**見證**：

- 自動測試：`tests/test_route.py`（透過子程序實際呼叫指令，搭配真的 Core）、`tests/test_intake.py`（鎖交接、取消、請求不符）。
- 真實執行：2026-10-20 用真的 agy，verifier 以專屬帳號執行。從交件到 COMPLETED 花 35 秒，Core VERIFIED。關閉接新任務（drain）後重送，拿回的是同一個任務，在途任務數為 0。見 `docs/drills/2026-10-20-route.json`。

## Gateway 這一端（還沒做；由 Gateway／部署器負責人決定誰改）

1. **分流點**：在 `nexus_worker_candidate` 的 handler（`unified_mcp_gateway_parts/worker_candidate.py`，約第 211 行），輸入驗證和 owner_confirmation 之後、`build_contract`／`submit_task` 之前。
   - 只有在環境變數開關打開、而且任務屬於試點類型（小 bug 修正）時，才轉給 Cairnwork。
   - 冪等鍵用 Gateway 的 task_id。
   - 綁定成功後，寫一筆 `<state>/cairnwork_jobs/<task_id>.json`，記下 Cairnwork 的 task_id。照 assist 工作的前例做（`assist_jobs.py:83`）。
2. **查詢、等待、取消**：`nexus_task_status`／`wait`／`cancel` 先查 `cairnwork_jobs`，查得到就呼叫 `cairnwork-route`，查不到才交給 SelfHostedTaskService（同 assist 的做法，`unified_mcp_gateway.py:3313-3339`）。
3. **失敗時不換系統**：
   - `submit` 回 `NOT_ACCEPTING` 時，這筆任務還沒綁定，照舊走原系統。這是回退後的正常路徑，不算換系統重跑。
   - 一旦拿到 Cairnwork 的 task_id，之後任何錯誤都只回報，不得再交給 SelfHostedTaskService。
4. **部署器的靜止閘門**：在 `tasks_in_flight` 的探測裡加上 `cairnwork-route in-flight`。`count > 0`、或指令出錯，都視為「有在途任務」（fail closed）。部署器的程式碼是受保護路徑，要另開一張 PR。
5. **環境變數**：Cairnwork 開關、`cairnwork-route` 的絕對路徑、state 目錄這三個，要加進部署器和 `mcp_gateway_durable.py` 的白名單。
6. **影子階段**：開關有第三個值「影子」，只呼叫 `check` 並記錄結果，不轉送任何任務（F 條：影子驗證不得產生外部副作用）。

## 切換與回退順序

1. Gateway 端改動合併並部署，開關設為「影子」。觀察 `check` 的紀錄，確認試點類型判斷正確。
2. Cairnwork `accept`，Gateway 開關改為「轉送」。
3. **線上見證**：一筆真實的小 bug 任務經過 Gateway 進入 Cairnwork，結果是 COMPLETED 而且 Core VERIFIED，同時 Gateway 的 status 查得到。
4. **回退演練**：
   - Cairnwork `drain`：新任務回到原系統，在途任務在 Cairnwork 跑完，或由 `recover` 處理。
   - Gateway 開關關閉。
   - 確認三件事：drain 後 `submit` 回 `NOT_ACCEPTING`；已轉送的任務仍查得到；沒有任何任務在兩個系統都跑。
