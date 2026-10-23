# 事故應變

Cairnwork 是公開 repo。一次資安事件就可能毀掉整個產品，所以先把「出事時誰做什麼」寫下來。

## 回報

不要開公開 Issue。外部回報請用 GitHub 的私密漏洞回報（repo 的 **Security → Report a vulnerability**）。內部 session 寄 coord 信給整合負責人。

## 情境一：憑證外洩（token、key、密碼進了 repo 或 log）

1. **先作廢憑證，再清 repo。** 只要曾經 push 到公開 repo，就當作已經被看到，刪掉 commit 也沒有用。
2. 到發行該憑證的服務作廢它，換一把新的。新憑證放進環境變數或本機設定，絕不進 repo。
3. 查那段時間的使用紀錄（GitHub audit log、服務端存取紀錄）。
4. 寫事故紀錄：外洩的範圍、作廢時間、怎麼進來的，以及要加什麼防線才不會再發生。

push protection 已經開啟，可以擋下大部分常見格式。但它擋不住自訂格式的 token，也擋不住 log 裡的憑證。

## 情境二：惡意或可疑的 PR

1. 不要在本機執行它的程式碼，也不要用 `pull_request_target` 跑它的程式碼。
2. 關閉 PR，必要時封鎖帳號。
3. 如果它已經被合併：
   - revert；
   - 檢查 workflow 檔案有沒有被改過；
   - 作廢 CI 能讀到的所有憑證。

## 情境三：線上任務做出越權的事（改了不該改的檔、推了不該推的分支）

1. 停止 Cairnwork 接新任務（F 條：回退時先停止接新任務）。
2. 在途的任務不要換系統重跑，由原系統恢復或明確終止。
3. 依任務的歸屬紀錄，找出所有受影響的任務和副作用，逐一還原。

## 平時

- **隱藏測試保密只在 `test_source: "bundle"` 成立**（私有存放區、沒匯入就拒絕、絕不退回 origin）。`auto`（程式預設）是相容／示範模式，沒有保密保證，任何文件和證明都不得在 `auto` 下主張。證明包的 `test_source` 欄位記錄該次執行用的模式（`bundle`／`auto`／`none`）。

- CI 只用最小權限（`permissions: contents: read`）。
- 不在 repo 裡寫主機路徑、內部 IP、帳號 email。
- 依賴固定版本。升級要用獨立 PR。
- **worker 帳號隔離（state `agy_run_as_user`，#183）是帳號邊界，不是完成的證明。** 設了之後 agy 模型程序跑在
  `_cairnwork_worker` 帳號下（經調度器、`sudo` 與 `agy-worker-exec` wrapper），讀不到 owner 的家目錄、agy 帳號池、
  Cairnwork 的 repo／log 和其他 attempt。它**不**保證：run 之間的分隔（同一個 uid，靠調度器的 per-run Seatbelt
  profile）、調度器本身的權限（仍是 owner）、也不會讓 worker 的輸出變得可信（候選仍只靠快照與 verifier 判定）。
  「確實以 worker 帳號執行」只認調度器的 worker 收據（`WORKER_LAUNCH`／`WORKER_SWEEP`／`WORKER_CLEANUP`），
  沒有收據、帳號不符、清掃不乾淨、或最後一行不是 `completed`，attempt 一律失敗，不會退回以 owner 身分執行；
  owner 無法對 worker uid 送訊號，收據沒證明已乾淨清掃並刪除的 run，不論調度器怎麼結束，都依 `WORKER_LAUNCH` 收據的 run 目錄
  （且 `run_root_id` 相符）請 wrapper `--sweep`，
  清不乾淨就是 `WORKER_SWEEP_UNCLEAN`，run 目錄保留；每次的判定留在 `<attempt>.agy.worker.json`。
  收據本身仍是調度器（owner 身分）寫的，證明的是調度器的說法，不是獨立見證。
  **只能在含 Nexus-new commit `4f526e8ca`（PR 2903）的調度器上啟用**：更舊、但已有 `--run-as-user` 的調度器
  會把 worker 的 stderr 原樣轉出，worker 能偽造一整組收據而通過；Cairnwork 沒有可靠的版本訊號可擋，這是部署前提。
  情境三發生在 worker 帳號下時，先查 `/Users/Shared/cairnwork-worker/worker-runs/` 有沒有留下的 run。
  細節與設定見 `docs/ISSUE_INTAKE.md`。
- **verifier 的通過是證據，不是完成的證明。** 候選程式碼和測試跑在同一個程序，判決在候選碰得到的
  地方產生，能讀環境的候選仍可偽造。一個任務是否真的完成，信任分層在 verifier 以外：Core VERIFIED
  綁定確切 commit、獨立審查、線上見證、真實使用、以及在 A/B 贏過裸骨幹。
- **驗證按風險分級。** 現行是 L0（同程序 + 受信任 plugin）+ L3（上述非技術信任）。要真正擋住**刻意**
  偽造判決，把 `assert` 搬到候選碰不到的監督端（裁判隔離，對可序列化 I/O 的測試用既有
  `cairnwork-verifier` 帳號即可，不必服務化）。選級由風險決定。細節見 `docs/PRESERVED_BEHAVIORS.md`
  的「完成的信任不在 verifier」與「驗證按風險分級」。
