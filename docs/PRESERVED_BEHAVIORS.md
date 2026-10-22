# 保留行為清單（試點：小 bug 修正）

依議會 D 條建立：從試點任務的實際呼叫路徑整理出 Cairnwork 必須保留的必要保障。目標是 30 條以內，但不能為了湊數而刪掉必要保障。

- **參考版本**：所有列的來源 commit 都是 Nexus-new `6ba3fc7d8`（origin/main，也是 2026-10-19 線上 Gateway 正在跑的 commit），所以不再逐列重寫；「來源」欄的行號都以這個 commit 為準。
- **路徑**：`eia_nightly.py`、`nexus_mcp_gateway_http.py` 在 `scripts/ops/`；`external_intelligence*.py`、`agy_account_pool.py` 在 `nexus/services/`；`self_hosted_task_service.py`、`canonical_core_transport.py`、`worktree_manager.py` 在 `nexus/orchestrator/`。
- **優先級**：P0＝議會點名、或少了就可能做出越權或假完成的保障；P1＝強化或防呆。
- **試點走哪條線**：Gateway 線。舊 Gateway 只把一類新任務轉給 Cairnwork，任務格式沿用 eia-task，要等 Core VERIFIED 才算完成。
- **EIA 夜間修 bug 那條線不換掉。** 它不走 Gateway，也不經過 Core。下表標「EIA」的條目是從那條線借來的保障，Cairnwork 要自己做出同樣效果。
- **狀態欄**：
  - `planned`：還沒實作。
  - `部分`：已有一部分（儲存格內寫明缺什麼），缺口各有 Issue 連結（2026-10-21 校正；先前 #2、#3、#22 誤標為 planned）。
  - `test:<id>`：已經有驗收測試。
- 每一條實作時，PR 都要把「Cairnwork 位置」和「驗收測試」兩欄補上。

| # | 優先 | 保障 | 要保證什麼 | 來源（Nexus-new） | Cairnwork 位置 | 驗收測試 |
|---|---|---|---|---|---|---|
| 1 | P0 | 入口認證 | bearer 用 `hmac.compare_digest` 比對；token 必填；只聽 loopback；請求 ≤1 MiB | `scripts/ops/nexus_mcp_gateway_http.py:57-69, 246-290, 316-318` | `src/cairnwork_protocol/ingress.py`（`check_bind`、`require_token`、`authorized`、`read_body`、`make_handler`；核心外，目前核心沒有任何網路入口） | `test:tests/test_ingress.py`（`test_check_bind_refuses_non_loopback`、`test_token_comparison_uses_compare_digest`、`test_http_length_statuses`、`test_core_has_no_hidden_network_entry` 等） |
| 2 | P0 | 可信身分才能派工 | 任務來源（Issue 作者、label 事件、後續編輯）都是可信身分，派工前和每次 attempt 前都重新檢查 | EIA `scripts/ops/eia_nightly.py:1079-1137` | 部分：`src/cairnwork/issue_task.py` `queue_trusted`、`issue_runner.py` `GitHub.trust`／`_take_one`（接手時檢查）。缺：派工前與每次 attempt 前重驗（#57） | `test:tests/test_issue_task.py::test_trusted_queue`、`test_untrusted_queue`、`test_the_latest_label_event_counts`；`tests/test_issue_runner.py::test_untrusted_or_malformed_issues_are_not_taken`。缺口測試 planned（#57） |
| 3 | P0 | 嚴格解析任務 | 只能有一個 eia-task 區塊、key 固定、allowed_files ≤10、不能有 `..`、不能碰保護路徑 | EIA `eia_nightly.py:246-325` | 部分：Issue 路徑已做（`issue_task.py` `parse_task_block`／`is_protected`／`pytest_argv`）；`pipeline.py` `check_task` 擋路徑形狀與 verifier 輸入。缺：Gateway submit 路徑沒套保護路徑（bug #51） | `test:tests/test_issue_task.py::test_a_good_block_parses`、`test_refused_blocks`。缺口測試 planned（#51） |
| 4 | P1 | 路徑真實性 | allowed_files 必須是一般檔案，或位於既有目錄下的新檔；不能是 symlink | EIA `eia_nightly.py:1143-1155` | planned（#60） | planned（#60） |
| 5 | P0 | verifier 白名單 | 只接受 `python3 -m pytest ... tests/*.py` 和 `git diff --check`；`shell=False` | EIA `eia_nightly.py:328-358`；`external_intelligence_closure.py:890-985` | `src/cairnwork/verifiers.py` `parse_verifier` | `test:tests/test_verifiers.py::test_allowed_verifiers_parse`、`test_other_verifiers_are_rejected` |
| 6 | P0 | 送件冪等（RULE-506） | 同一個 key、同一個 hash：回傳既有任務；同一個 key、不同 hash：拒絕；同一張 card 出現第二個 task id：`DUPLICATE_LOGICAL_TASK`。CANCELLED／SUPERSEDED 釋放 card；FAILED 要先標 SUPERSEDED 才能用新 key 重試 | `nexus/orchestrator/self_hosted_task_service.py:2061-2089` | `src/cairnwork/ownership.py` `OwnershipLedger.bind` | `test:tests/test_ownership.py::test_identical_resend_returns_the_same_task` 等 6 項 |
| 7 | P0 | 任務歸屬紀錄 | 派工前用原子且持久的紀錄綁定冪等鍵、請求摘要、所屬系統、task id；歸屬不明就停止派工（議會 F 條，新規定） | — | `src/cairnwork/ownership.py` `OwnershipLedger.bind/route/set_status` | `test:tests/test_ownership.py::test_resend_cannot_move_the_task_to_another_system`、`test_unknown_task_raises_instead_of_guessing_an_owner`、`test_concurrent_binds_of_one_key_create_exactly_one_task` |
| 8 | P0 | 防重送 | attempt 以 `request_sha256` 為 key；已完成或已失敗：`REPLAY_FORBIDDEN`；進行中：`RECONCILIATION_REQUIRED` | EIA `nexus/services/external_intelligence.py:1928-1946, 2028-2046` | `src/cairnwork/ownership.py` `OwnershipLedger.bind`（`_refuse_replay`，與 bind 同一個 `BEGIN IMMEDIATE` 交易，用程式檢查、不用唯一索引，舊狀態目錄仍能開啟）；`route.py` 把 `REPLAY_FORBIDDEN`／`RECONCILIATION_REQUIRED` 都對應 exit 2；SUPERSEDED／CANCELLED 可換 key 重送。同 key／同 card 的部分見 #6 | `test:tests/test_replay.py`、`tests/test_replay_route.py`（#55） |
| 9 | P1 | 派工去重 | 每個任務、每個單元只派一次；安全重試最多一次；session 綁定任務、單元、workspace、worker | EIA `external_intelligence_fanout.py:672-706, 2112-2125` | 無自動重試（FAILED 要人工 SUPERSEDED）、非 BOUND 不執行（`pipeline.py` `run_bound_task` 回 `NOT_BOUND`）、worker 綁 `CAIRNWORK_ATTEMPT`、attempt clone 不重用。重現（兩個 runner 搶同一個 BOUND 任務）顯示 worker 只跑一次，但 journal 會多一個 attempt；已在 `journal.py` 加每任務一筆 DISPATCHED 的部分唯一索引，輸家回 `NOT_BOUND`（`pipeline.py` `_execute`） | `test:tests/test_dispatch_once.py`（#55） |
| 10 | P1 | 來源血緣 | `main_sha` 必須是 tracking main 的祖先，remote 必須相符 | EIA `external_intelligence_automation.py:579-610` | 部分：Issue 路徑每輪 fetch 並以 `origin/main` 為 base，`_test_base`、`_open_pr` 驗祖先。缺：clone origin 與 `--repo` 的比對（bug #53），以及 submit 路徑的 base 祖先檢查（#59） | planned（#53、#59） |
| 11 | P0 | worktree 隔離 | 每個 attempt 都在 `expected_base_sha` 開新的 detached worktree；路徑衝突、tree 不乾淨、HEAD 漂移一律拒絕 | EIA `external_intelligence_fanout.py:574-599`；`external_intelligence_closure.py:633-658` | `src/cairnwork/workspace.py` `create_attempt_clone`（獨立 `git clone --no-local`，worker 碰不到 Cairnwork 的 repo）；`src/cairnwork/gitcmd.py`（停用 hook／fsmonitor、忽略 replace refs、清掉 GIT_*） | `test:tests/test_workspace.py::test_clone_is_a_detached_checkout_of_base_with_its_own_git_dir`、`test_clone_shares_no_object_files_with_cairnworks_repository`、`test_worker_config_commands_never_run`、`test_worker_cannot_move_cairnworks_refs` |
| 12 | P0 | 擷取時再驗範圍 | 範圍不能擴大；碰到禁止路徑或超出 `max_files` 就擋；worker 不能移動 HEAD（HEAD 漂移的檢查見 #11 引用的 fanout/closure 行號） | EIA `external_intelligence_fanout.py:1605-1647, 1839-1879`；`worktree_manager.py:2909-2942`（只有範圍與禁止路徑的計算） | `src/cairnwork/workspace.py` `collect_candidate` | `test:tests/test_workspace.py::test_out_of_scope_change_is_rejected`、`test_deletion_needs_permission`、`test_symlink_is_rejected_even_inside_scope` |
| 13 | P0 | 完整性檢查 | 對 base 跑 diff-check、刪除檢查、範圍檢查，三項都要存在而且通過，不依賴任務自己宣告的 verifier | EIA `external_intelligence_closure.py:251-344, 1415-1422` | `src/cairnwork/workspace.py` `collect_candidate`（diff --check、刪除、範圍） | `test:tests/test_workspace.py::test_whitespace_errors_are_rejected` 等 |
| 14 | P0 | verifier 執行紀律 | timeout；HEAD 固定；tree 必須乾淨；verifier 動到 workspace 就算錯；returncode 0 才算通過 | EIA `external_intelligence_closure.py:890-985` | `src/cairnwork/verifiers.py` `run_verifiers`（在 candidate 的全新 clone 上跑、最小環境、timeout、跑後比對 tree） | `test:tests/test_verifiers.py::test_verifier_that_writes_into_the_checkout_fails`、`test_timeout_fails`、`test_inherited_secrets_do_not_reach_the_verifier`、`test_verifier_observations_lead_to_core_verified` |
| 15 | P1 | 收據綁定 | 重新計算 receipt id、tree、diff hash，驗證 id 要一致 | EIA `external_intelligence_closure.py:741-860` | 部分：Core 請求的雜湊都由 Cairnwork 自 git 重算，`notary.py` `_is_verified` 比對回聲。缺：推送前以重算的 change set 對照公證請求（#61） | planned（#61） |
| 16 | P0 | Core VERIFIED 才算完成（RULE-690） | 只有 nexus-certify 回 `VERIFIED` 才能進入完成；非 200 回應就是 `FAILED_VERIFICATION` | `nexus/orchestrator/canonical_core_transport.py:1265-1343` | `src/cairnwork/notary.py` `Notary.notarize`／`_is_verified`；`src/cairnwork/core_request.py` | `test:tests/test_notary.py::test_in_scope_change_with_passing_verifier_is_verified`、`test_core_rejections_are_never_verified`、`test_verified_status_alone_is_not_enough`、`test_forged_verified_response_with_other_hashes_is_not_verified`、`test_verified_attempt_cannot_be_replayed_for_another_change`、`test_reader_must_hold_the_verified_commit` |
| 17 | P0 | 公證紀錄不可覆寫 | 每次驗證結果都保留，後一次不能蓋掉前一次（Nexus-new 用 `INSERT OR REPLACE`，transport:1321，這裡不照抄） | 反例：`canonical_core_transport.py:1321` | `src/cairnwork/notary.py`（UNIQUE(task_id, attempt_id)＋禁止 UPDATE/DELETE 的 trigger＋recursive_triggers） | `test:tests/test_notary.py::test_failed_attempt_stays_failed_and_core_is_called_once`、`test_record_is_append_only_for_any_connection`、`test_tampered_stored_request_hash_is_not_verified` |
| 18 | P0 | 失敗不會變成功 | 必要保障、驗證或 Core 呼叫失敗時，不會產生成功狀態或越權副作用；允許的降級要在契約裡列明 | 議會 E 條；EIA `external_intelligence_closure.py:45, 1529-1549` | `src/cairnwork/notary.py` `_call_core`（Core 例外或格式錯誤一律記錄為未驗證） | `test:tests/test_notary.py::test_core_exception_is_recorded_and_not_verified`、`test_malformed_or_error_responses_are_not_verified` |
| 19 | P0 | 推送前再驗一次 | 只能有一個 commit、parent 是 `main_sha`、只新增或修改 allowed_files 裡的一般檔案 | EIA `eia_nightly.py:1856-1962` | `src/cairnwork/workspace.py` `collect_candidate` | `test:tests/test_workspace.py::test_two_commits_are_rejected`、`test_replace_refs_cannot_disguise_the_candidate`、`test_any_difference_between_clone_and_candidate_is_rejected`、`test_no_commit_is_rejected` |
| 20 | P0 | 有 TTL 的 Core binding | binding 固定來源 commit、tree、workspace、授權；有 `valid_until`；第一個副作用之前重新驗證 | EIA `external_intelligence_core_binding.py:552-666, 785-881` | planned（#63，批次 3；核心行數預算不足，需做成核心外套件） | planned（#63） |
| 21 | P0 | 重啟恢復 | 副作用進行中當掉時，變成 `RECONCILIATION_REQUIRED` 並留下 effect id；重啟後先對帳再繼續；恢復依 #7 歸屬紀錄路由 | EIA `external_intelligence_automation.py:1576-1589` | `src/cairnwork/recovery.py`、`journal.py`、`runlock.py`（不自動重跑；只有 Core 對 journal 記下的候選判定 verified 才補成 COMPLETED） | `test:tests/test_recovery.py`（真的 kill -9 每一個時間點） |
| 22 | P0 | 取消 | worker 還活著就拒絕取消；否則記錄 `CANCELLED` 並清理；取消依 #7 歸屬紀錄路由 | `self_hosted_task_service.py:4035-4053` | 部分：`src/cairnwork/intake.py` `Intake.cancel`（只有 BOUND 能取消，compare-and-set；DISPATCHED 拒絕）。缺：沒有檢查任務歸屬（bug #52） | `test:tests/test_intake.py::test_a_task_not_started_can_be_cancelled_and_is_then_never_run`、`test_cancel_loses_to_a_runner_that_dispatched_first`。歸屬檢查測試 planned（#52） |
| 23 | P0 | 共享資源租約 | worktree、帳號等共享資源要有租約和 TTL，到期自動釋放；程序重啟後舊的持有者不能繼續寫入（議會 F 條，新規定） | 參考：`agy_account_pool.py:91-119` 只有 flock 租約與 `release()`；**Nexus-new 沒有 TTL 與舊持有者隔離**，這兩項是新規定 | 部分：`runlock.py`（flock，持有者死亡即釋放）。缺：`verifier_account.exclusive()` 無逾時（23a，#62）；TTL 與 fencing（23b，#63） | planned（#62、#63） |
| 24 | P1 | PR 內文只放驗證過的欄位 | PR 內文與 coord 訊息只放經過格式驗證的欄位（`publication_tokens`）；寫 GitHub 一律用 bot 身分（Nexus-new 靠安裝設定，不在這段程式裡） | EIA `eia_nightly.py:533-547` | planned（#58）；已有：PR 內文只含 Issue 號、task id、驗證過的 attempt 與 commit（`issue_runner.py` `_open_pr`） | `test:tests/test_issue_runner.py::test_a_strange_attempt_id_is_never_pushed`（部分）；其餘 planned（#58） |
| 25 | P0 | 完成上限 | Cairnwork 自己不合併；worker 沒有核准、整合、推 main 的權限 | EIA `eia_nightly.py:377-428` | 部分：結構上沒有 merge／approve 呼叫，`draft_pr` 固定 `--draft --base main`，只推 `cairnwork/<issue>-<attempt>`（`issue_runner.py`）。缺：鎖住這些保證的測試（#56）；worker 與擁有者同帳號的缺口見 #19 | planned（#56） |

## 不帶過去的

依 Nexus-new 的缺陷分流，以下判定為死碼、沒有負責人或只是操作 CLI，不列入：RULE-004、RULE-199、RULE-219、RULE-220、RULE-252。

## 還沒決定的

- verifier 的 sandbox：已用 macOS Seatbelt 封裝，預設全部拒絕，禁止連網，只能讀系統、Python 和 checkout，只能寫 checkout 和這次執行的目錄。缺少 sandbox-exec 時直接判失敗。每一種攻擊都有見證測試（`tests/test_sandbox.py`），macOS CI 一定會實際執行，不能跳過。**還沒解決的**：
  - 候選程式碼和 pytest 在同一個程序裡，仍然可以偽造報告。判決已經不再讀候選可寫的
    `junit.xml`，改由受信任的 pytest plugin 在 `sessionfinish` 寫一份帶 nonce 的紀錄
    （`src/cairnwork/report_guard.py`，PR #21，保留行為 #14），這擋掉了「讀 argv 的 junit 路徑寫假檔」這條，但
    **是部分緩解，不是邊界**：候選讀得到執行環境裡的 nonce，仍可以寫一份相符的紀錄再結束。
    對「跑任意候選碼、看測試過不過」這個模型，in-process 的候選沒有拿不到的秘密，容器也只隔離
    主機（容器內仍可偽造），所以**技術上無法讓判決不可偽造**。因此 verifier 的通過是**證據，
    不是完成的證明**。
  - sandbox 裡的程式可以用 sysctl `KERN_PROCARGS2` 讀到同一個使用者底下其他程序的參數和環境變數，裡面可能有 token。Seatbelt 規則擋不住。**已用專屬帳號解決（Issue #9）**：傳入 `VerifierAccount` 時，verifier 以 `cairnwork-verifier` 身分執行，macOS 不允許跨使用者讀取這些參數，見證測試在 `tests/test_verifier_account.py`。沒有傳入帳號時（例如 Linux）缺口仍在，所以 verifier 的 log 一律不交給 worker 或模型。
  - 自己開新 session、又清掉環境變數的子程序：帳號模式下，執行結束後會以該帳號執行 `kill -9 -1` 清掉它的所有程序，有任何殘留就判失敗；沒有帳號時仍然躲得過清除。帳號模式一次只能跑一個 verifier（獨占鎖）。每次開跑前，也會先確認該帳號底下沒有任何程序。
  - 專屬帳號保護的是擁有者不被 verifier 讀取，反方向不成立：sudoers 允許擁有者以該帳號執行任意 profile 的 sandbox-exec，所以任何以擁有者身分執行的程序，都能以該帳號執行程式碼。該帳號本身沒有任何特權，不能用來提升權限。
  - Seatbelt 只能用在 macOS，不是可以移植的正式 sandbox。
  - Linux bwrap 後端（P2，`src/cairnwork_sandbox/bwrap.py`）：用「唯讀綁定根 + 具名遮蔽憑證」——整個根目錄唯讀綁定、只有寫入根可寫，所有 namespace 都隔離（無網路），丟掉全部 capability，禁止巢狀 user namespace；具名憑證目錄（`~/.ssh`、`~/.aws`、`~/.gnupg`、`~/.config`、`~/.docker`、`~/.kube`、`~/.claude`、`~/.codex`、`~/.gemini`、`~/.nexus`、`~/.local/state`、`~/.local/share/keyrings`、`~/.terraform.d`）與 socket 樹（`/run/user`、`/tmp/.X11-unix`、`/run/libvirt`——涵蓋所有 libvirt daemon socket）用空 tmpfs 蓋掉；憑證檔（`~/.netrc`、`~/.git-credentials`、`~/.npmrc`、`~/.pypirc`、`~/.pgpass`、`~/.my.cnf`、`~/.vault-token`、`~/.Xauthority`）與 root 等價 daemon socket（Docker／Podman／containerd／系統 D-Bus／LXD／Incus）用 `/dev/null` 蓋掉（唯讀綁定擋不住 connect() 到 pathname socket，network namespace 只蓋 abstract socket）。**取捨**：P4 專屬帳號上線前，sandbox 以「跑它的帳號」身分執行，所以 sandbox 內可讀範圍 ＝ 該帳號可讀範圍，只靠遮蔽清單擋已知憑證，不是完整隔離（Seatbelt 的「只能讀白名單」在 Linux 沒有對應）；其他使用者的 0700 家目錄由 POSIX 權限擋住。Seatbelt（macOS）行為不變。見證測試同一份 `tests/test_sandbox.py`，Linux CI（`test-linux-sandbox`）在真的 bwrap 上實跑；「讀寫入根以外的檔案被拒」這兩項見證只適用 Seatbelt。
- Gateway 怎麼把一類任務轉給 Cairnwork：Cairnwork 端的 `cairnwork-route` 已完成（`docs/GATEWAY_ROUTE.md`）；Gateway 端的分流、靜止閘門和開關由部署器負責人和整合負責人另外訂，F 條的「先驗證、晚切換」照做。

## 完成的信任不在 verifier

verifier 只負責產生證據。一個任務是否真的完成，信任來自 verifier 以外、分層的機制，任何一個都不宣稱單獨足夠：

- **Core VERIFIED 綁定確切候選 commit**（#16）：沒有這顆章不算完成。
- **獨立審查**：另一個不同模型家族的審查者，實際寫 probe 重現，不只讀碼。
- **線上見證 / canary**：在真實入口觀察，而不是只看離線的 verifier 報告。
- **真實使用**：對外接口（`cairnwork-route` / Issue intake）的實際流量。
- **在 Cairnwork 自己的 A/B 贏過裸骨幹**：試點成敗以此為準，不看功能數或 PR 數。

## 驗證按風險分級

「便宜」和「擋得住偽造」不能完全兼得，所以驗證按風險分級，不是一套打死。假完成也不是單一維度，
至少三種正交的作弊各需要不同防線：

- **劫持裁判**（候選讀 nonce、`os._exit`、改測試檔）→ 靠**裁判隔離**與**禁改測試**。
- **偷看考卷／過擬合**（讀測試硬編答案、偵測測試環境只在測試時表現正確）→ 靠**隱藏／擾動測試**。
- **語義假修好**（吞 exception、只過 happy path、留副作用）→ 靠**測試品質**與**真實使用**。

分級（選哪級由任務風險、worker 可信度、有無外部介面決定）：

- **L0（現行）**：in-place 單元測試 + 受信任 plugin（PR #21）。便宜，但判決在候選的程序內產生，
  能讀環境的候選仍可偽造——是證據，不是證明。
- **L1**：加 per-test 子程序，縮小一個測試污染其他測試的範圍。
- **L2（裁判隔離）**：把 `assert` 搬到候選碰不到的監督端。有兩種：**函式級**——監督者（本機擁有者身分）
  握答案與斷言，候選函式在無權限子程序（既有的 `cairnwork-verifier` 帳號）執行、只回傳值，對
  「可序列化輸入輸出」的測試**不需外部介面、用現有帳號即可做**；**整場黑箱**——候選只能透過外部
  介面（CLI/HTTP）被測，需要有這樣的介面。兩者都只關「劫持裁判」，關不住過擬合與語義假修好。
- **L3（永遠開）**：見上「完成的信任不在 verifier」。完成的信任最終在這；但 L3 不是免證明的補強，
  獨立審查可能共享盲點、真實使用可能事後才發現、A/B 勝出不等於指定 bug 已修好，需各自量化抓弊率。

現況：試點停在 **L0 + L3**。函式級 L2 是便宜且近期可行的強化（不必等服務化），優先序與是否採用
由整合負責人定。推演、議會審查與取捨見 knowledge `nexus/research/2026-10-20-forgeable-test-report.md`。

試點實測（#2769，首次）：兩次都 `FAILED_VERIFICATION`，但**不是 worker 的能力問題**——查明是
Cairnwork verifier 自己的環境問題（sandbox 擋了 `/usr/bin/git`，xcrun shim 讀不到
`libxcrun.dylib`），讓那 5 個測試在**未修改的 main** 上也失敗；再加上 teeth 檢查過寬，把這些環境
失敗也算成「測試在 base 失敗」。worker 第二次的修正，其實與在沙箱外驗證過能讓 19 個測試全過的修法
一致。兩個缺陷由整合負責人修復中。**教訓：verifier 失敗必須先排除環境因素**（在未修改的 base 上跑
一次對照），否則會把環境 bug 誤判成 worker 或候選的問題——這也提醒，上面各級防線都假設 verifier
本身是對的。
