# Issue 直接送 Cairnwork（試點任務來源）

2026-10-20 James 決定：試點的真實任務直接從 GitHub Issue 進 Cairnwork，不必繞舊 Gateway（線上 Gateway 近 30 天只處理了 28 件任務，全是 agent 的探測任務）。

2026-10-21 James 決定：
- **小而明確的 bug（符合 eia-task 形狀、非 hard）預設走這條**，不再另外決定。每天跑一到兩輪 `run-once`；累積的真實使用數據也作為 A/B 的 A1 來源。
- **連續 10 張沒有 Cairnwork 環境失敗**之後，才把 `run-once` 掛成 launchd 無人值守。
- **每個新 repo 先跑 `scripts/env_smoke.py`**，把環境耦合一次列出來再送題。驗收指令只能挑在主機上**沒有 skip** 的測試（verifier 只要有任何 skipped 就判失敗）。
- 舊 Gateway 的 route 停車（見 `GATEWAY_ROUTE.md` 開頭）。

## 怎麼送

1. 在 Issue 內文放**一個** ```` ```eia-task ```` JSON 區塊，格式和 Nexus-new 的 EIA 相同（`docs/ops/eia_nightly.md`）。有三點不同：
   - `verification` 只接受 `python3 -m pytest [-q] [-p no:cacheprovider] tests/...`，不接受 `git diff --check` 那種 shell 寫法。
   - `hard` 的 Issue 不收。
   - **驗證用的測試檔不能列在 `allowed_files`**。Cairnwork 不讓 worker 改驗證會讀的檔案，以防它把測試改成永遠通過。
   - **證明 bug 的測試用 `test_commit` 提供（測試延後套用）**：開 Issue 的人先推一個分支，上面只有一個 commit，父 commit 是 main 上的某個版本，內容只新增或修改 `tests/` 底下的一般檔案，不能有 `conftest.py`。把它的 40 位 hex 填進區塊的 `test_commit`。
     - **worker 在修 bug 的期間，這台機器上沒有任何地方有這個測試**：
       - 接手時只透過 GitHub API 讀取它的父 commit，不下載測試。
       - 給 worker 的指示裡不含 `eia-task` 區塊，也不含 commit id。
       - worker 結束後，Cairnwork 才抓取測試；在 base 上跑的 log 用完就刪。
       - 任務開始前，如果這個測試 commit 已經在本機出現過（例如重試，或之前另一個任務用過同一個），就拒絕（`TEST_COMMIT_ALREADY_EXPOSED`）。重試時要把測試推成一個新的 commit。兩個**同時執行**的任務用同一個測試 commit 時擋不住，所以一個測試 commit 只給一張 Issue 用。
       - **限制**：worker 和 Cairnwork 用同一個系統帳號，而修正的驗證 log 是 Core 公證的證據，必須保留。所以重試時如果新的測試內容和上一次相同，worker 仍可能從上一次的 log 讀到。要徹底解決，必須讓 worker 改在獨立的帳號或環境裡執行（追蹤中）。
     - **確認測試有效**：在「base + 測試」上跑 verifier，JUnit 報告裡必須有延後測試檔的測試案例失敗，而且沒有 verifier 用其他方式出錯。測試原本就通過、只是 import 失敗、或只有別的檔案失敗，都拒絕（`TESTS_DO_NOT_FAIL_ON_BASE`）。
     - **驗證修正**：把測試疊到修正上，再跑 verifier、交給 Core 公證。PR 的內容就是這個 commit，修正和測試一起送出。
     - **base 的決定方式**：有 `test_commit` 時，base 是它的父 commit，而且必須在 origin/main 的歷史裡，不是當下的 main。
     - **限制**：測試分支放在公開 repo 上。worker 拿不到 commit id，也不會被告知分支名稱，但如果 worker 有網路，理論上能自己去找。要徹底杜絕，必須限制 worker 的網路，這要等 worker 改在獨立環境裡執行。分支名稱不要用一看就知道的名字。分支要保留到 Cairnwork 收尾（開出 PR 或判定失敗）之後才刪：worker 結束時 Cairnwork 才用 commit id 抓取測試，而 GitHub 只允許抓取還有分支指得到的 commit。
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
