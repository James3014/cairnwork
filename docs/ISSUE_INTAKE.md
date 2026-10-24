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
   - **`allowed_files` 不能需要新目錄（#149，fail closed）**：每個 allowed file 的上層目錄都必須已經在 base commit 裡、而且是真正的目錄（不能是檔案、symlink 或 submodule），否則送件時就拒絕 `TASK_PATH_PARENT_INVALID`（`route check` 也會預演這個拒絕）。要在新目錄裡加檔案，先在 base 上建立該目錄（例如先合併一個加 `__init__.py` 或 `.gitkeep` 的 commit，再以它當 base），或改列既有目錄底下的路徑。檔案本身可以還不存在（新檔），存在的話必須是一般檔案；驗證目標必須已在 base 上或列在 `allowed_files`（有 `test_commit` 的任務在送件時不比對驗證目標，因為測試 commit 要等 worker 結束才抓）。
   - **證明 bug 的測試用 `test_commit` 提供（測試延後套用）**：開 Issue 的人先推一個分支，上面只有一個 commit，父 commit 是 main 上的某個版本，內容只新增或修改 `tests/` 底下的一般檔案，不能有 `conftest.py`。把它的 40 位 hex 填進區塊的 `test_commit`。
     - **私有存放區（#119，worker 隔離的來源端）**：測試 commit 不必、也不該推到目標 repo 的公開 `cwt-*` 分支（有網路的 worker 可以自己去抓）。改成在作者的 clone 上建單一 commit 的 bundle：`git bundle create tests.bundle <branch> ^<base>`，再由跑 Cairnwork 的帳號執行 `cairnwork-verify --state <目錄> import-tests --repo-path <mirror> --bundle tests.bundle`。匯入時在 state 底下的暫存 repo 驗證並重建成正規 bundle（只有一個 head、單親、父 commit 必須已在 mirror 裡），存成 `<state>/deferred-tests/<sha>.bundle`（檔案 0600；state 本來就必須是 owner 的 0700，worker 帳號和其他人進不去）。已匯入、已在 mirror、或已被燒掉（`verify-used-tests`）的 commit 一律拒絕，不覆蓋。
     - 取用：接手時父 commit 從 bundle 讀（不經 GitHub）；worker 結束後 Cairnwork 才從 bundle 抓進自己的 clone。單次使用、「從未出現過」、父 commit 綁定和可信身分檢查都不變。
     - `config.json` 的 `test_source`：`"auto"`（預設，向下相容）有 bundle 就用，沒有才從 origin 抓（公開分支，worker 仍可能找得到）；**`"bundle"` 是嚴格模式**，沒匯入就拒絕（`TEST_COMMIT_NOT_IMPORTED`／`TASK_TEST_COMMIT_NOT_IMPORTED`），絕不退回 origin。第三輪 A/B 與 worker 帳號隔離上線（#121）時必須設 `"bundle"`。**只有 `"bundle"` 模式可以宣稱「worker 看不到隱藏測試」**；`auto` 只是相容／示範用（quickstart、demo），任何文件、報告與證明包都不得在 `auto` 下主張測試保密。證明包記錄 `test_source`（見 `docs/ATTESTATION.md`）。bundle 損壞或 state 目錄不安全（symlink、權限不是 0700）→ `TEST_COMMIT_UNREADABLE`／`TEST_STORE_UNSAFE`，不會產生成功狀態。
     - **worker 在修 bug 的期間，這台機器上沒有任何地方有這個測試**：
       - 接手時只透過 GitHub API 讀取它的父 commit，不下載測試。
       - 給 worker 的指示裡不含 `eia-task` 區塊，也不含 commit id。
       - worker 結束後，Cairnwork 才抓取測試；在 base 上跑的 log 用完就刪。
       - 任務開始前，如果這個測試 commit 已經在本機出現過（例如重試，或之前另一個任務用過同一個），就拒絕（`TEST_COMMIT_ALREADY_EXPOSED`）。重試時要把測試推成一個新的 commit。兩個**同時執行**的任務用同一個測試 commit 時擋不住，所以一個測試 commit 只給一張 Issue 用。
       - **限制**：worker 和 Cairnwork 用同一個系統帳號，而修正的驗證 log 是 Core 公證的證據，必須保留。所以重試時如果新的測試內容和上一次相同，worker 仍可能從上一次的 log 讀到。要徹底解決，必須讓 worker 改在獨立的帳號或環境裡執行（追蹤中）。
     - **確認測試有效**（三態，#234，規則 `base-compare-v1`）：在「base + 測試」上跑 verifier，看 guard 紀錄（不是 JUnit 檔）。每個 verifier 都判，不提早結束：
       - 有延後測試的案例失敗，而且每個 verifier 都正常跑完（exit 0 或 1、有 guard 紀錄、沒有收集錯誤）→ 有牙齒，繼續。
       - 每個 verifier 都正常跑完，但延後測試沒有失敗（原本就會過，或只有別的檔案失敗）→ 拒絕（`TESTS_DO_NOT_FAIL_ON_BASE`）。
       - 只要有一個 verifier 在 base 上沒辦法比較 → **UNCOMPARABLE**（沒有證據，不算通過也不算擋下），即使另一個 verifier 有牙齒：guard 記到收集錯誤 `BASE_COLLECTION_ERROR`（通常是 exit 2），exit 5 以外的其他非 0／1（沒有收集錯誤的 exit 2，例如被中斷；exit 3、4 等）`BASE_PYTEST_ERROR`，exit 5 `BASE_NO_TESTS`，逾時 `BASE_TIMEOUT`，沒有沙盒而沒執行 `BASE_NOT_EXECUTED`，報告超過上限 `BASE_REPORT_OVER_CAP`，沒有 guard 紀錄 `BASE_NO_REPORT`。P2P 的 base 探測不能用（`P2P_BASE_PROBE_UNUSABLE`）也是 UNCOMPARABLE；探測成功但沒有任何測試在 base 上通過，只記在 `p2p`（`targets: 0`），不是 UNCOMPARABLE。
       - UNCOMPARABLE 不送 Core、沒有 notary 紀錄、不簽收據。`cairnwork-verify` 回 `status: "UNCOMPARABLE"`；worker 路徑記 FAILED，reason 是 `UNCOMPARABLE: <碼>: <細節>`。**Issue 留言目前還不會顯示 UNCOMPARABLE**：照舊是一般的失敗留言、標成 `cairnwork:failed`；之後另開 PR，用派工時就存下來的分類來顯示「沒有證據」。base 執行都在 worker 之後，所以只有 worker **真的隔離**時才標 UNCOMPARABLE；否則 worker 有機會故意弄壞 base 或驗證帳號，這時照舊是一般的 FAILED（reason 是 `<碼>: <細節> (worker not isolated: <原因>)`，沒有 `UNCOMPARABLE:` 前綴）。**隔離的定義（PR-C）**：設了 `agy_run_as_user`，而且這個帳號查得到（`pwd`），它的 uid 不是 root、不是 owner（`os.getuid()`），也不是驗證帳號的 uid。只設了名稱但帳號不存在、或就是 owner／驗證帳號，都算沒隔離，原因寫進 reason。
       - **帳號在 base 階段出錯**（跑「base + 測試」或 P2P 探測時驗證帳號不能用、還有殘留行程、鎖等不到）：這時還沒有任何候選程式以驗證帳號執行過，所以算 UNCOMPARABLE `VERIFIER_ACCOUNT_UNUSABLE`（worker 路徑同樣只在隔離時加 `UNCOMPARABLE:` 前綴）。**候選自己的 verifier 執行時帳號出錯，一律不算 UNCOMPARABLE**（候選的程式碼已經以該帳號跑過，可能是它弄壞的）：worker 路徑是 `INFRA_ERROR`，`cairnwork-verify` 是內部錯誤（結束碼 1）。
       - **帳號預檢（PR-C）**：worker 開工之前先檢查驗證帳號能不能跑它的 Python（`VerifierAccount.available()` 決定），不能就直接結束：FAILED，reason `UNCOMPARABLE: VERIFIER_ACCOUNT_UNUSABLE: <原因>`，journal 沒有 `WORKER_STARTED`，不花 worker 額度、測試也沒有被看過（worker 還沒動，所以不受隔離規則限制）。原因會指出是哪一層目錄擋住，例如 `/Users/james 0750 james:staff not traversable by cairnwork-verifier`（`src/cairnwork/account_check.py` 逐層看 uid／群組的權限位元；它不看 ACL，所以只是說明，判定仍以 `available()` 為準）。
       - **寫延後測試的人注意**：修正才新增的函式或類別，要在**測試函式裡面** import（`def test_x():\n    from calc import new_thing`），不要放在檔案開頭。放在檔案開頭時，base 上這個檔案根本收集不到（exit 2），結果是 UNCOMPARABLE，不是有牙齒；放進函式裡，base 上是執行時失敗（exit 1），才算有牙齒。
     - **驗證修正**：把測試疊到修正上，再跑 verifier、交給 Core 公證。PR 的內容就是這個 commit，修正和測試一起送出。
     - **base 的決定方式**：有 `test_commit` 時，base 是它的父 commit，而且必須在 origin/main 的歷史裡，不是當下的 main。
     - **限制**：測試分支放在公開 repo 上。worker 拿不到 commit id，也不會被告知分支名稱，但如果 worker 有網路，理論上能自己去找。要徹底杜絕，必須限制 worker 的網路，這要等 worker 改在獨立環境裡執行。分支名稱不要用一看就知道的名字。分支要保留到 Cairnwork 收尾（開出 PR 或判定失敗）之後才刪：worker 結束時 Cairnwork 才用 commit id 抓取測試，而 GitHub 只允許抓取還有分支指得到的 commit。
2. 由可信身分貼上 `cairnwork:queue` 標籤。可信身分是 `James3014` 或 `james3014-nexus-agent[bot]`。
   - repo 是公開的，所以會逐一檢查：Issue 作者、最後一次貼標籤的人、貼標籤之後所有的標題和內文修改，都必須是可信身分。
   - 編輯歷史不完整也一律拒絕。
3. 執行 `cairnwork-issues --state <Cairnwork state> --repo James3014/<repo> --clone <專用 clone> run-once`。
   - **事前檢查**：`uv run python scripts/preflight.py --state <state> [--clone <專用 clone>]` 印一個 JSON，每項一個碼（`OK` 表示通過）：`config`（`STATE_MISSING`、`BAD_CONFIG`；失敗時其他項是 `SKIPPED`）、`verifier_python`（`VERIFIER_PYTHON_MISSING`）、`account`（`VERIFIER_ACCOUNT_UNUSABLE`，附 `walk`：每一層目錄的權限、擁有者和帳號能不能進入）、`mirror`（`MIRROR_MISSING`）、`clone`（`CLONE_NOT_STATE_MIRROR`）、`worker_isolation`（設了 `agy_run_as_user` 卻不算隔離時 `WORKER_NOT_ISOLATED`）。全部 `OK` 時 `ok: true`、結束碼 0，否則結束碼 1。它不燒測試、不 fetch、不綁任務；帳號那一項和每次 verifier 執行一樣，會拿驗證帳號的鎖（最多 `--lock-wait` 秒，預設 60）並以該帳號跑一次探測。
   - **`--clone` 必須就是 state 設定的 `mirror`**（`config.json` 的 `mirror`，Gateway 路徑用的同一個 clone）。啟動時就檢查（#234 PR-C）：兩者的 realpath 不同就以結束碼 3、`{"error": "CLONE_NOT_STATE_MIRROR"}` 拒絕，什麼都不抓。指向 mirror 的 symlink 可以；同一個 origin 的另一個 clone、或從 mirror 複製出來但設定已經改過的 clone 都不行。state 沒設 `mirror` 時不檢查（這時任務本來就用 `--clone`）。worker 在 mirror 裡跑，延後測試疊上去的 commit（journal 的 `CANDIDATE`、Core 公證的那個 commit）只存在於 mirror（`refs/cairnwork/attempts/<id>-tested`）；指向另一個 clone 時找不到這個物件，開 PR 前的 `merge-base` 檢查就失敗，不會推出任何東西（#229）。
   - **專用 clone(mirror)一定要單分支**：用 `git clone --single-branch --branch main <url>` 建立。整包 clone 會把放延後(隱藏)測試的單 commit 分支也抓進 mirror,讓 worker 本機就讀得到。runner 啟動時會檢查 `remote.origin.fetch` 只含 main,否則以 `LINEAGE_MIRROR_NOT_SINGLE_BRANCH` 拒絕(#76)。
   - **接手**：符合條件的 Issue 交給 Cairnwork，改標成 `cairnwork:running`，並留言附上 task id。
   - **收尾**：
     - 完成而且 Core 判定 VERIFIED：推出分支 `cairnwork/<issue>-<attempt>`，開成 **draft PR**。PR 內文寫 `Refs #N` 並附上 Nexus 的標記，Issue 標成 `cairnwork:pr-opened`。
     - 沒有完成：在 Issue 留言寫明原因，標成 `cairnwork:failed`。不會自動重跑；要重試，就修改區塊內容，那會成為一件新任務。
   - **格式不符**：留言一次說明原因，改標成 `cairnwork:needs-fix`，移出佇列。
   - **身分不可信**：照 EIA 的做法完全不碰，也不留言。
   - **Cairnwork 停止接新任務（drain）時**：Issue 留在佇列，下一輪再試。

## 在 Issue 任務裡用私有測試 bundle（取代推 `cwt-*` 分支）

要宣稱「worker 看不到隱藏測試」的任務，照這個順序做：

1. 作者在自己的 clone 建單一 commit 的測試分支（父 commit 是 main 上的某個版本，只動 `tests/` 底下的一般檔案），**不要推到目標 repo**。
2. `git bundle create tests.bundle <branch> ^<base>`。
3. 由跑 Cairnwork 的帳號匯入：`cairnwork-verify --state <目錄> import-tests --repo-path <mirror> --bundle tests.bundle`（細節與拒絕條件見下方「私有存放區」）。
4. state 的 `config.json` 設 `"test_source": "bundle"`（沒匯入就拒絕，絕不退回 origin）。
5. Issue 的 `eia-task` 區塊把該 commit 的 40 位 hex 填進 `test_commit`，其餘照常。
6. 要出證明時，確認證明包 `test_source` 是 `"bundle"`（它是該次執行時用的模式，之後改 config.json 不會改變它）（`cairnwork-attest verify --json` 會印出）；是 `"auto"` 或 `"none"` 的證明不能拿來宣稱測試保密。

## 保證與限制

- 用的是和 Gateway 轉送相同的 state，所以 `in-flight` 兩邊的任務都算得到；停止接新任務（`drain`）也同時對兩邊生效。
- 冪等鍵由 Issue 號碼加上區塊雜湊組成：重複執行不會重複派工，也不會重複留言。
- base 是專用 clone 的 `origin/main`（每次都先 fetch），不使用任何共用的工作目錄。
- Cairnwork **不合併 PR**。draft PR 照一般規則審查，檢查全綠後由審查者合併。
- 推送之前會再檢查一次：commit 格式、是否為 base 的後代、改動的檔案都在 `allowed_files` 裡。開 PR 之前會先查有沒有既有的 PR，所以中途失敗後重跑也不會重複開。
- 同一時間只會跑一輪（鎖）。收尾時不會重新檢查可信身分（EIA 會），因為 PR 內容完全不用 Issue 的文字。
- 目標 repo 的測試需要該 repo 自己的依賴和 pytest 設定。依賴：用 `scripts/setup_target_venv.sh` 建 venv，並在 state 的 `config.json` 設 `verifier_python`。一個 state 目錄只有一個 `verifier_python`，所以 `--repo` 一個 repo 就要配一個自己的 state 目錄（見 `docs/GATEWAY_ROUTE.md`）。pytest 設定一律取自 base commit，由 `src/cairnwork/pytest_config.py` 處理。
- **測試目錄（`test_roots`）**：很多專案的測試放在套件目錄裡（例如 `pkg/tests/`），不是最上層的 `tests/`。state 的 `config.json` 可設 `"test_roots": ["pkg/tests"]`（相對目錄的清單，預設 `["tests"]`，最多 8 個）。它同時決定三件事：verifier 目標（`python3 -m pytest -q pkg/tests/test_x.py`，不在清單內的目錄一律拒絕 `VERIFIER_NOT_ALLOWED`）、延後測試 commit 只能新增或修改哪些目錄下的檔案、以及候選不可改的測試檔（每個根目錄底下的檔案都算，`conftest.py` 等保護檔名在任何位置都照舊不可改，也不可放進測試 commit）。每一項都要是乾淨的相對路徑，只含英數、底線、連字號和 `/`，每一段不能以 `-` 開頭，最後一段必須剛好是 `tests` 或 `test`（區分大小寫，所以設定不會讓延後測試 commit 改到正式程式）：不能是空的、`.`、絕對路徑、含 `..`、結尾斜線、萬用字元，也不能落在保護路徑（例如 `.github`）；違反就是 `BAD_CONFIG`。執行時還會檢查 base commit：根目錄路徑上的每一段若存在，必須是真的目錄，是符號連結或檔案就拒絕 `TEST_ROOT_NOT_A_DIRECTORY`。延後測試 commit 裡也不能有候選不可改的檔名：`conftest.py`、`pytest.ini`、`setup.cfg`、`tox.ini`、`pyproject.toml`、`setup.py`、`sitecustomize.py`、`usercustomize.py`、`*.pth`；`__init__.py` 仍允許（只有測試自己 import 套件時才會執行）。沒設時行為與以前完全相同。一個 state 目錄只對應一個目標 repo，所以這個設定也是一個 repo 一份。
- **來源血緣（`require_base_on_main`）**：state 的 `config.json` 可設 `"require_base_on_main"`（布林，預設 `true`）。新任務綁定前，Cairnwork 會 fetch 任務 repo 的 origin main，base commit 不在 origin/main 的歷史裡就拒絕 `BASE_NOT_ON_MAIN`；fetch 失敗則是另一個錯誤碼 `ORIGIN_FETCH_FAILED`（fail closed）。沒有 origin 的 repo 要在 config.json 設 `require_base_on_main` 為 `false`。重送已綁定的任務不重查。這是提交時防呆的檢查（擋誤送），不是防惡意呼叫者：路由路徑不跑 `verify_origin`。
- **帳號池調度器（`agy_dispatch`）**：多個程序共用一池 agy 帳號的主機上，state 的 `config.json` 可設 `"agy_dispatch": "/絕對路徑/調度器"`（必須是絕對路徑，否則 `BAD_CONFIG`）。設了之後 worker 不直接呼叫 `agy`，改執行這個調度器，由它選帳號、持有租約、額度用完時換帳號。固定的參數形狀由測試鎖定：`--cwd=<attempt 目錄> --mode=accept-edits --model=<model> --timeout=<秒> --pool-wait-timeout=<秒> --max-calls=1 --provider-stall-seconds=<秒> --stream-no-progress-seconds=<秒>`，再加每個允許檔案一個 `--write-path=<檔名>`（一律用 `=` 連接，所以以 `-` 開頭的值不會被當成選項；`model` 必須是單行字串，否則 `BAD_CONFIG`）；提示詞從 stdin 送入，沒有 `--sandbox`（寫入範圍由調度器依 `--write-path` 限制）。環境變數與以前相同的白名單，外加以 `NEXUS_AGY_` 開頭的調度器調校變數；其他變數一律不傳。調度器的 stderr 不寫入磁碟，worker 邊讀邊只留最後一行（最長 64 KiB） `NEXUS_AGY_DISPATCH {json}`：結束碼 75 且 `status` 為 `pool_exhausted`（整池都沒額度）時，任務失敗原因是 `WORKER_FAILED: AgyPoolExhausted: POOL_EXHAUSTED: ...`，不會換模型也不會退回直接呼叫 `agy`；逾時時先對調度器的程序群組送 SIGTERM，最多等 15 秒讓它釋放租約，再照舊 SIGKILL（直接呼叫 `agy` 的路徑不變）；其他失敗（找不到或不可執行、逾時、非零結束碼）沿用 `WORKER_FAILED: AgyWorkerError`。時間參數（整數秒，1 到 86400）：`agy_dispatch_pool_wait_seconds`（預設 600）、`agy_dispatch_provider_stall_seconds`（預設 900）、`agy_dispatch_stream_no_progress_seconds`（預設 900）；模型執行總時間仍是 worker 的 900 秒。沒設時 argv 與行為跟以前完全相同（測試鎖定）。
- **worker 帳號（`agy_run_as_user`，#19 Phase B 呼叫端，#183）**：設了 `agy_dispatch` 之後，`config.json` 可再設 `"agy_run_as_user": "_cairnwork_worker"`（只能是帳號名稱：字母、數字、`_`、`-`，最長 64 字，否則 `BAD_CONFIG`；沒設 `agy_dispatch` 時設它也是 `BAD_CONFIG`，因為只有調度器能換帳號）。設了之後 worker 多傳 `--run-as-user=<帳號>` 給調度器，由調度器（仍以 owner 身分選帳號、持有租約）把 agy 本身跑在那個 macOS 帳號下：每次 attempt 在 `/Users/Shared/cairnwork-worker/worker-runs/<32 位十六進位 run tag>/` 複製一份 clone（不含 `.git`），經 `sudo -n -u <帳號> /Users/Shared/cairnwork-worker/bin/agy-worker-exec` 加 Seatbelt 執行，結束後把改動複製回 attempt clone（一次性 sudo 設定見 `scripts/setup_worker_user.sh`；調度器端見 Nexus-new #2884）。**有的隔離**：模型程序讀不到 owner 的家目錄、agy 帳號池、Cairnwork 的 repo／log 和其他 attempt；attempt clone 的快照規則不變（仍只收允許的檔案）。**沒有的隔離**：每個 run 共用同一個 worker uid，run 之間靠調度器的 per-run Seatbelt profile 分隔，不是靠檔案權限；調度器本身仍以 owner 身分執行。**證據：worker 收據**（調度器最低版本：Nexus-new commit `4f526e8cab743f036ae556b27a1e86c0456bc564`，PR 2903／#2888）。**只能在含這個 commit 的調度器上啟用 `agy_run_as_user`**：有 `--run-as-user`（fcdbc278b）但沒有 PR 2903 的調度器會把 worker 的 stderr 原樣轉出，worker 可以自己印一整組看起來正確的收據而通過檢查，Cairnwork 分不出真假，也沒有可靠的版本訊號可以拒絕舊版，所以這是部署前提，不是執行時檢查。收據格式：調度器每個 attempt 在 stderr 寫三行（前綴同狀態行 `NEXUS_AGY_DISPATCH`）：`WORKER_LAUNCH`（`run_as_user`、`run_root`、`run_root_id`、`wrapper_sha256`）、`WORKER_SWEEP`（`result`、`wrapper`、`left`）、`WORKER_CLEANUP`（`run_root_removed`、`worker_failure` 只留 `KIND:reason`），最終狀態行仍在最後；worker 自己印出的同形狀行會被調度器加上 `[provider]` 前綴，Cairnwork 不讀。worker 讀**所有**調度器行（最多 64 行、每行最多 64 KiB；超過行數、某行被截斷、或讀取失敗都當收據不完整；超過 64 KiB 的行其餘部分一律丟到換行為止，所以 worker 不能把收據藏在超長行的後段），只有下列全部成立才算隔離成功：每個有收據的 attempt 三行各一次、`WORKER_LAUNCH` 的帳號等於設定的帳號、`run_root` 是 `/Users/Shared/cairnwork-worker/worker-runs/<32 位十六進位>`、`WORKER_SWEEP` 是 `clean`／`CLEAN`／`left` 空、`run_root_removed` 為真、沒有 `worker_failure`；結束碼 0 時最後一行還必須是 `completed` 狀態，且它的 attempt 有 `WORKER_LAUNCH`。否則 attempt 失敗（`AgyWorkerAccountFailed`），原因碼：`WORKER_RECEIPT_MISSING`（沒有收據，例如調度器沒照旗標做）、`WORKER_RECEIPT_MISMATCH`（帳號或 run 目錄不對）、`WORKER_RECEIPT_DUPLICATE`、`WORKER_RECEIPT_INCOMPLETE`、`WORKER_RECEIPTS_UNKNOWN`、`WORKER_SWEEP_UNCLEAN`、`WORKER_RUN_ROOT_LEFT`、`WORKER_ACCOUNT_FAILED`（收據點名的 `worker_failure`）、`DISPATCH_STATUS_MISSING`（最後一行不是 `completed`，例如 Popen 失敗）。每次都在 agy log 旁留 `<attempt>.agy.worker.json`（owner 限定）：讀到的收據、事後清掃結果、找到的問題。**失敗即失敗（fail closed）**：調度器回報帳號、wrapper 或複製回來失敗（`failure_kind` 含 `AGY_WORKER_`／`RUN_AS_USER_`，不論結束碼），attempt 失敗原因是 `WORKER_FAILED: AgyWorkerAccountFailed: WORKER_ACCOUNT_FAILED: <failure_kind>`，絕不改以 owner 身分執行 agy；不認得這個旗標的舊版調度器會以結束碼 2 拒絕，同樣是失敗。**清理**：補不補清只看收據，不看調度器怎麼結束：收據證明已乾淨清掃（`clean`／`CLEAN`／`left` 空）且 `run_root_removed` 的 run 不再補清（這時 wrapper 的 `--sweep` 也會因目錄不存在拒絕，結束碼 64）；其他每個有 `WORKER_LAUNCH` 的 run，不論是逾時被殺、被別人以訊號殺掉、崩潰、輸出被佔住、或帶著 `failed` 最終狀態正常結束，都會事後補清，attempt 仍然失敗。owner 無法對另一個 uid 送訊號，worker 從調度器已寫出的 `WORKER_LAUNCH` 收據取得 run 目錄（不讀程序表猜；就算其他行遺失或讀取失敗，解析成功的收據照樣補清，attempt 仍然失敗）。收據沒顯示「已乾淨清掃並刪除」、目錄還在、而且目錄的 (dev, ino) 仍等於收據的 `run_root_id`（避免同一路徑已換成別的 run）的，就以 `sudo -n -u <帳號> /Users/Shared/cairnwork-worker/bin/agy-worker-exec --sweep <run_root>` 請 wrapper 殺掉那個 run 的 worker 程序，只有 wrapper 回報 `AGY_WORKER_SWEEP clean`（結束碼 0）才刪掉留下的 run 目錄（裡面有該次租用帳號的 agy session 檔）；清不乾淨、wrapper 拒絕、收據帳號或 `run_root_id` 不符、被訊號殺掉前沒有任何 `WORKER_LAUNCH` 收據（launch 情況不明）、或目錄已不在但沒有乾淨的 `WORKER_SWEEP`，attempt 失敗原因是 `WORKER_SWEEP_UNCLEAN`，run 目錄保留待人工處理。沒設時 argv 與行為跟只設 `agy_dispatch` 時完全相同（測試鎖定）。部署注意：第三輪 A/B 量測進行中不要在線上 state 啟用（其 PREREG 固定 worker 與 owner 同 uid），等第三輪定案後再開。
- **重送邊界（`REPLAY_FORBIDDEN`）**：同一份已綁定的請求（`request_digest`）換冪等鍵或換 card 再送，已完成或已失敗的回 `REPLAY_FORBIDDEN`，進行中的回 `RECONCILIATION_REQUIRED`，都不會重跑。所以編輯過區塊、但綁定的請求內容沒變的 Issue，每一輪都會被擋下（安全，不會重跑）；要重做就換新的測試 commit，或真的改動請求內容（FAILED 的舊任務要先人工 SUPERSEDED）。
- 和 EIA 不重複：`cairnwork:queue` 是另一個標籤。要做 A/B 比較時，再明確把同一張 Issue 兩個標籤都貼上，兩邊各自開 PR，最後只合併一張。

## 常設 KPI：7 天返工率

每一張由 Cairnwork 從 Issue 交付、且已合併的 PR（下稱「原 PR」），都要記「7 天返工率」。它是第一層指標，和一次成功率（送進件的 Issue 中，第一次執行就產出可合併 PR 的比例）一起報告，不單獨拿來下結論。

- **定義**：以 PR 數計。分母是合併已滿 7 天（窗口已走完）的原 PR；分子是其中至少有一個「返工 PR」的原 PR。
- **時間窗**：比的是後續 PR 的合併時間，落在原 PR 合併時間之後、7×24 小時以內（含整點）。
- **判定順序**：窗口內每一個後續已合併 PR，依序逐條判定，第一條成立就停：
  1. 是還原（revert）原 PR 的 commit → 算。
  2. 只改格式（空白、換行、import 排序、formatter 輸出），沒有其他改動 → 不算。
  3. 和原 PR 改過的行有任何一行重疊（以 git blame 追到原 PR 的 commit 為準，程式碼和非程式碼檔都一樣）→ 算，不管有沒有引用原 Issue／PR。
  4. 內文、commit 訊息或留言引用原 Issue／PR，而且說是要修正它 → 算，即使改的是別處。
  5. 其他（包括只是提到原 PR、沒說要修正它）→ 不算。
- **記錄**：每次交付一列，放在該 state 目錄的交付紀錄裡（欄位：repo、Issue、原 PR、合併時間、返工 PR 編號或空白、判定日期）。合併滿 7 天後補填一次，不回頭重算。交付紀錄的格式由 Cairnwork 整合負責人定義。

## 驗證別處產生的 commit：`cairnwork-verify`

候選 commit 不是 Cairnwork 的 worker 做的（例如另一個 agent 或 CI 產生）時，用同一套檢查判定它（程式是核心的一部分：`src/cairnwork/verify_candidate.py` 與 `verify_cli.py`）：

```
cairnwork-verify --state DIR run --repo-path P --base SHA --candidate SHA \
  --allowed-file F [--allowed-file F ...] --verifier "python3 -m pytest -q tests/..." [...] \
  [--test-commit SHA]
cairnwork-verify --state DIR cleanup     # 某次執行被強制結束後，在 recover 之前跑
```

- 檢查和 worker 的產出完全相同：候選必須是 base 上的單一 commit、只動 `allowed_files`（不能刪檔、不能有 symlink 或 submodule）；給了 `--test-commit` 就在 worker 之後才疊上延後測試（同樣的「從未出現過」和單一父 commit 規則）；verifier 在沙盒裡跑（同樣的白名單，任何 skipped 都算失敗）；最後交給 Core 公證，回應必須綁定這個 commit。
- 只印一個 JSON：`status`（`VERIFIED`、`FAILED`、`REFUSED`、`UNCOMPARABLE`）、`verified`、`reason`、`core_status`（Core 自己回的字，是否通過以 `verified` 為準）、`judged_commit`、`executed_tree`、`binding`、`uncomparable`（UNCOMPARABLE 時是 `{code, detail, rule: "base-compare-v1"}`，其他時候是 `null`）、`tests_consumed`（`true` 表示 `--test-commit` 現在已經登記為用過，重試要推一個新的測試 commit；沒給 `--test-commit` 時是 `false`）。結束碼：0 通過，2 拒絕、不可比較或未通過（看 `status`），3 用法錯誤，1 內部錯誤。
- **帳號預檢與 `tests_consumed`（#234 PR-C）**：設了驗證帳號時，任何測試 commit 被登記用過（燒掉）之前，先確認帳號能跑它的 Python；不能就回 `status: "UNCOMPARABLE"`、`uncomparable.code: "VERIFIER_ACCOUNT_UNUSABLE"`（`detail` 指出擋住的那一層目錄）、`tests_consumed: false`，測試 commit 沒燒，修好帳號後可以用同一個測試 commit 重跑。燒掉之後才出現的 UNCOMPARABLE（base 不能比較、P2P 探測失敗、base 階段帳號出錯）都是 `tests_consumed: true`。輸出絕不會因此變成通過（`verified` 仍只看 Core）。
- **不相容的新值（#234）**：`UNCOMPARABLE` 是新的 `status`，表示 base 本身沒辦法比較（見上方「確認測試有效」），沒有證據，既不是通過也不是擋錯。以前這些情況回 `REFUSED`（`TESTS_DO_NOT_FAIL_ON_BASE` 或 `P2P_BASE_PROBE_UNUSABLE`）。用 `status == "REFUSED"` 判斷「擋下了」的呼叫端要另外處理 `UNCOMPARABLE`，不能把它算成擋錯或通過；只看結束碼的呼叫端不受影響（仍是 2）。
- **收據綁定（#229）**：給了 `--test-commit` 時，verifier 跑的是暫存 clone 裡「候選＋延後測試」的 commit（`judged_commit`）。這個 commit 只存在於暫存 clone，跑完就刪，而且它的 id 含 commit 時間，別人重算不出來。所以 Core 公證的對象改成它的 **tree**（`target_revision = git-tree:<executed_tree>`），收據主體是 **PR head（候選本身）**，`binding` 記下 `rule`（疊合規則 `overlay-v1`）、`base`、`head`、`test_commit`、`test_paths`、`executed_tree`、verifier 指令（`verifiers`，不含 P2P 選出的額外測試）。`sha256(binding)` 取代 Core 請求的 `acceptance_contract.requirements_hash`，Core 回應會回傳這份 contract 的 hash，換掉或拼接 binding 都對不上。notary 紀錄升到 schema 3，多一欄可為 NULL 的 `binding_json`（用 `ALTER` 遷移，舊列是 NULL、照舊驗）。之後 `cairnwork-attest statement --candidate <候選>` 簽出 v0.2 收據，收件方離線驗證的方式見 `docs/ATTESTATION.md`「External candidates」。沒給 `--test-commit` 時 `judged_commit` 就是候選，`binding` 是 `null`，一切照舊（綁 commit、v0.1 收據）。這個變更之前簽的外部候選收據（主體是暫存 commit）依然驗不了。
- 和 `submit` 一樣：state 沒有 `ACCEPTING`（drain 中）就拒絕（`VERIFY_NOT_ACCEPTING`），拿到執行鎖之後會再檢查一次。
- 所有檢查都在這次執行專用的暫存 clone（`<state>/verify-scratch/`，結束就刪）裡跑：從 state 設定的 `mirror`（沒設就從 `--repo-path`）clone，再把 base 和候選從 `--repo-path` fetch 進暫存 clone。外部的候選**不會**進共用的 mirror，也不會寫進呼叫者的 repo（所以不會讓之後的測試 commit 看起來「已出現過」）。
- `--test-commit` 的處理和 worker 路徑相同：先對 mirror（沒設就是 `--repo-path`）做「從未出現過」檢查，已存在就拒絕（`TEST_COMMIT_ALREADY_EXPOSED`）；測試 commit 從該 repo 的 `origin`（相對路徑會先換成絕對路徑）抓進暫存 clone，疊在候選上後一起驗證，暫存 clone 結束時刪除。**測試 commit 一旦被一次 verify 取用（抓取之前就先用 `O_EXCL` 建立 `<state>/verify-used-tests/<sha>`，所以任何候選程式碼執行之前就登記了，崩潰或之後被拒絕也算用過），就視為已外洩（候選的程式碼和 log 看得到它），之後同一個 commit 一律拒絕 `TEST_COMMIT_ALREADY_EXPOSED`，要換新的測試 commit。只遮掉 log 不夠，因為通過或失敗本身就會洩漏測試內容，所以測試 commit 只能用一次；父 commit 不對的候選在登記之前就被拒絕，燒不掉它。**
- 不跑 worker，也不在 ownership 帳本建立任務，所以 `in-flight`（部署器的靜止檢查）看不到它；它持有共用的執行鎖，所以 `recover` 這時會回 `RUN_LOCK_BUSY`。Core 紀錄寫進 state 的 notary，任務代號以 `verify-` 開頭。
- 執行期間在 `<state>/verify-runs/` 放一個加鎖的標記檔；行程被強制結束時核心的 `recover` 看不到它（沒有帳本記錄），改用 `cleanup`：它清掉已死執行的殘留 clone、暫存 clone、基準目錄和孤兒 verifier 行程，還活著的執行（鎖還在）不動。每次 `run` 開頭也會先做一次。**強制結束後請先跑 `cleanup`，再跑 `recover`**，否則孤兒 verifier 會讓 `recover` 以為驗證帳號不閒。注意：設了驗證帳號時，`cleanup` 沒有權限結束以該帳號執行的行程（它只清得掉自己啟動的行程和目錄）；那些行程由 `recover` 的閒置檢查（`require_idle`）處理。
- 候選必須能從 `--repo-path` 取得（先推到一個分支或 fetch 進來），而且在 `--repo-path` 裡就先檢查它是 base 上的單一 commit（否則 `NOT_ONE_COMMIT_ON_BASE`），之後才 fetch 或做任何其他事；蓋在隱藏測試 commit 上的候選因此在任何東西被抓取之前就被拒絕，不會讓該測試 commit 變成「已出現過」。

## Upgrading Core

The notary compares every stored verdict with the Core identity it is configured to
expect (`core_identity` in `config.json`, else the Core installed beside Cairnwork), at
read time. After a Core upgrade, rows recorded under the old Core read as not verified
(`CORE_IDENTITY_MISMATCH`). Drain in-flight tasks before upgrading, and update the pin
together with the Core.
