# Issue 直接送 Cairnwork（試點任務來源）

2026-10-20 James 決定：試點的真實任務直接從 GitHub Issue 進 Cairnwork，不必繞舊 Gateway。舊 Gateway 這條線照議會 F 條繼續接完，用來證明切換和回退機制可靠。理由是：查過線上 Gateway 近 30 天只處理了 28 件任務，全部都是 agent 送的探測任務，沒有真實工作。

## 怎麼送

1. 在 Issue 內文放**一個** ```` ```eia-task ```` JSON 區塊，格式和 Nexus-new 的 EIA 相同（`docs/ops/eia_nightly.md`）。有三點不同：
   - `verification` 只接受 `python3 -m pytest [-q] [-p no:cacheprovider] tests/...`，不接受 `git diff --check` 那種 shell 寫法。
   - `hard` 的 Issue 不收。
   - **驗證用的測試檔不能列在 `allowed_files`**。Cairnwork 不讓 worker 改驗證會讀的檔案，以防它把測試改成永遠通過。
   - **證明 bug 的測試用 `test_commit` 提供（測試延後套用）**：開 Issue 的人先推一個分支，上面只有一個 commit，父 commit 是 main 上的某個版本，內容只新增或修改 `tests/` 底下的一般檔案，不能有 `conftest.py`。把它的 40 位 hex 填進區塊的 `test_commit`。
     - worker 只拿到父 commit（base），看不到這個測試。
     - Cairnwork 先在「base + 測試」上跑 verifier：至少要有一個以 exit 1 失敗，而且沒有其他錯誤，才算測試有效。測試原本就通過，或者只是 import 失敗，都拒絕（`TESTS_DO_NOT_FAIL_ON_BASE`）。
     - worker 修完後，Cairnwork 把測試疊到修正上，再跑 verifier、交給 Core 公證；PR 的內容就是這個 commit（修正加測試）。
     - 有 `test_commit` 時，base 是它的父 commit（必須在 origin/main 的歷史裡），不是當下的 main。
2. 由可信身分貼上 `cairnwork:queue` 標籤。可信身分是 `James3014` 或 `james3014-nexus-agent[bot]`。
   - repo 是公開的，所以會逐一檢查：Issue 作者、最後一次貼標籤的人、貼標籤之後所有的標題和內文修改，都必須是可信身分。
   - 編輯歷史不完整也一律拒絕。
3. 執行 `cairnwork-issues --state <Cairnwork state> --repo James3014/<repo> --clone <專用 clone> run-once`。
   - **接手**：符合條件的 Issue 交給 Cairnwork，改標成 `cairnwork:running`，並留言附上 task id。
   - **收尾**：
     - 完成而且 Core 判定 VERIFIED：推出分支 `cairnwork/<issue>-<attempt>`，開成 **draft PR**。PR 內文寫 `Refs #N` 並附上 Nexus 的標記，Issue 標成 `cairnwork:pr-opened`。
     - 沒有完成：在 Issue 留言寫明原因，標成 `cairnwork:failed`。不會自動重跑；要重試，就修改區塊內容，那會成為一件新任務。
   - **格式不符**：留言一次說明原因，改標成 `cairnwork:needs-fix`，移出佇列。
   - **身分不可信**：照 EIA 的做法完全不碰，也不留言。
   - **Cairnwork 停止接新任務（drain）時**：Issue 留在佇列，下一輪再試。

## 保證與限制

- 用的是和 Gateway 轉送相同的 state，所以 `in-flight` 兩邊的任務都算得到；停止接新任務（`drain`）也同時對兩邊生效。
- 冪等鍵由 Issue 號碼加上區塊雜湊組成：重複執行不會重複派工，也不會重複留言。
- base 是專用 clone 的 `origin/main`（每次都先 fetch），不使用任何共用的工作目錄。
- Cairnwork **不合併 PR**。draft PR 照一般規則審查，檢查全綠後由審查者合併。
- 推送之前會再檢查一次：commit 格式、是否為 base 的後代、改動的檔案都在 `allowed_files` 裡。開 PR 之前會先查有沒有既有的 PR，所以中途失敗後重跑也不會重複開。
- 同一時間只會跑一輪（鎖）。收尾時不會重新檢查可信身分（EIA 會），因為 PR 內容完全不用 Issue 的文字。
- 目標 repo 的測試需要該 repo 自己的依賴和 pytest 設定。依賴：用 `scripts/setup_target_venv.sh` 建 venv，並在 state 的 `config.json` 設 `verifier_python`。一個 state 目錄只有一個 `verifier_python`，所以 `--repo` 一個 repo 就要配一個自己的 state 目錄（見 `docs/GATEWAY_ROUTE.md`）。pytest 設定一律取自 base commit，由 `src/cairnwork/pytest_config.py` 處理。
- 和 EIA 不重複：`cairnwork:queue` 是另一個標籤。要做 A/B 比較時，再明確把同一張 Issue 兩個標籤都貼上，兩邊各自開 PR，最後只合併一張。
