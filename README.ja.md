# ロボットの街：規則を自分たちで直す（コース D）

[English](README.md) | 日本語

教材ページ：[コース D — ロボットの街](https://www.ertl.jp/cadl-spec/ja/docs/handson/robot-town-tutorial)

3 社の配送ロボット（各社 2 台）が、ラズパイマウスのシミュレータと同じ
11 ノードの道路網を共有する。道路のルールと顧客への約束は CADL
（`cadl/robot_town.cadl`）に書かれ、シミュレーション結果を見て改正していく。

改正のループは 2 つある。

| ループ | 目標を決めるのは | 改正案を出すのは | 決めきれないことは |
|---|---|---|---|
| `town.steward`（[後述](#人間が-kpi-を決めllm-が提案するtownsteward)） | 人間が自然言語で書く KPI（`kpi.md` か `kpi.en.md`、期ごとに変えてよい。評価方法と達成基準を箇条書きで添えてもよい） | LLM（規則・規制・契約） | LLM が判断要求にまとめ、人間が答えて、理由を自然言語か GSN で残す |
| `town.council` | `constitution.yaml` の `objective`（固定） | ルールベースの探索、または LLM（規則だけ） | 判定で機械的に決まる（否決されれば採らない） |

- 規範の判定（期限・監視・状態遷移）は、コース A と同じ Python 参照
  ランタイム（`town/contract_engine.py`）が IR を読んで行う。
- 走っているロボットは交差点を通り抜けられる。交差点に止まっている
  ロボット（積み下ろし中・道路上で待機中）がいると、誰も入れない。
- 各社の行動方針は `town/policies.py`。A 社は協調型、B 社は期限ぎりぎり
  まで道路で待つ計算型（罰点がなければ居座る）、C 社は居座る強欲型（罰点の
  倍率が上限の 1.0 になると、期限を 15 秒過ぎたところで離れる）。
- 運用モードは分散（DISTRIBUTED）と集中（CENTRALIZED）の 2 つ。切り替える
  条件は CADL の `sos.transitions` に書かれる（後述）。

## セットアップ

コース A のセットアップ（`~/program/cadl_repo` に CADL コンパイラをクローン）が
済んでいることが前提。CADL コンパイラは 0.3.32 以降を使う（0.3.39 で確かめている。判断の記録の書き出しには 0.3.39 以降が要る）。
`cadl --version` で確かめられる。古いコンパイラの IR には切替の安全条件が入らないので、街は規則を却下する。
手順とインストールの問題への対処は
[テキストブックの 2.4 節](https://www.ertl.jp/cadl-spec/ja/docs/textbook/toolchain-setup/#24-セットアップ-5-分)にある。

```bash
cd ~/program
git clone https://github.com/ertlnagoya/cadl-robot-town
cd cadl-robot-town
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip   # macOS の python3（3.9）の pip は古く、次の行が失敗する
pip install -e ~/program/cadl_repo     # cadl コマンドと PyYAML が入る
pip install anthropic                  # LLM を実際に呼ぶとき（town.steward、--proposer llm）だけ
```

## 1 期だけ動かす（分散モードに固定）

```bash
python -m town.period --mode DISTRIBUTED
```

```text
需要: 各社の注文の平均間隔 75 秒
運用モード: DISTRIBUTED
規則: robot_town.cadl  (hold_limit=60s, block_k=3, sanction_lambda=0.5)
  定時配達 121.8 / 注文 132.2  未配達 0.0  ふさがれた時間 2404s  （シード平均）
  会社  定時率  遅配  道路で待った回数  ふさがれた秒数  罰点   効用
   A     0.84   7.8               0.0            1352    0.0   38.4
   B     0.94   2.6              38.0             849    0.9   38.9
   C     1.00   0.0              39.6             203   14.8   28.8
  違反（原因別，シード平均）: deadline:dropoff=2.6, deadline:pickup=7.8, deadline:release=23.0, monitor:blocking_guard=2.8
  運用モード: 集中の時間 0%  切替 0.0 回（切替中の合計 0s，管理者による退去 0.0 台，安全条件の違反 0.0 回）
```

C 社は交差点に居座って定時率 100% を得て、そのぶん A 社がふさがれる。

## 改正ループを実行する

```bash
python -m town.council --mode DISTRIBUTED            # 1 期
python -m town.council --mode DISTRIBUTED --rounds 6 # 6 期
```

改正案は `constitution.yaml` の `amendable` にあるパラメータ（CADL の
`# @amend:<名前>` 行）を 1 つだけ 1 ステップ動かしたもの。検査・憲法・
改善・投票の 4 つの判定を通った案のうち、目標が最もよくなる案を採択する。

```text
=== 第 1 期（平常：注文の平均間隔 75 秒）  現行 v1: sanction_lambda=0.5, hold_limit=60, block_k=3, to_central=22, to_distributed=26 ===
  改正案                                  定時配達  違反 (道路) ふさがれた  検査 憲法 改善 投票(A B C)
  hold_limit 60s → 50s                     120.6  40.4 (28.8)     2468s    ○    ○    ×   ○ × ×
  hold_limit 60s → 70s                     124.2  31.6 (23.6)     2367s    ○    ○    ○   ○ ○ ○
  block_k 3 → 2                            131.0  35.2 (34.0)      985s    ○    ○    ○   ○ × ×
  block_k 3 → 4                            119.4  36.4 (23.6)     2788s    ○    ○    ×   ○ × ○
  sanction_lambda 0.5 → 0                  119.0  59.0 (45.8)     3059s    ○    ○    ×   ○ ○ ○
  sanction_lambda 0.5 → 1                  132.2  23.6 (23.6)      845s    ○    ○    ○   ○ ○ ×
  ...
  → 採択: sanction_lambda 0.5 → 1（目標 +10.4，賛成 AB）。v2.cadl として保存。
```

罰点の倍率を 1 に上げると、C 社は期限を 15 秒過ぎたところで交差点を離れる
ようになり、ふさがれた時間は 2404 秒から 845 秒に減る。罰点が増える C 社は
反対するが、A・B 社の賛成で定足数（2 社）に届く。待たされる台数 2 で退去させる案
（`block_k 3 → 2`）も定時配達を増やすが、居座りで得をしている B・C 社の反対で
否決される。罰点をなくす案（`sanction_lambda 0.5 → 0`）には 3 社とも賛成するが、
B 社も期限を気にせず居座るようになり、定時配達が減るので改善の判定で除かれる。

## 需要の変化（scenario.yaml）

第 k 期の需要は `scenario.yaml` の `periods[k-1]` で決まる。平常・繁忙期・
閑散期があり、平常と繁忙期の 2 期目には、期の途中（20〜40 分）に注文が
集中するラッシュがある。期ごとに乱数シードも変わり、現行の規則もその期の
環境で評価し直してから改正案と比べる。

```bash
python -m town.period --interval 45                 # 繁忙期だけを 1 期
python -m town.period --rush 1200 2400 25           # 平常＋ラッシュを 1 期
```

分散モードに固定して 6 期実行すると（`--mode DISTRIBUTED --rounds 6 --name seasons-d`）、
次のようになる。

```text
- 第 1 期（平常）: v2 sanction_lambda 0.5 → 1（目標 +10.4，賛成 AB）
- 第 2 期（平常＋ラッシュ）: 採択なし
- 第 3 期（繁忙期）: v3 hold_limit 60s → 70s（目標 +2.0，賛成 AC）
- 第 4 期（繁忙期＋ラッシュ）: v4 hold_limit 70s → 80s（目標 +1.8，賛成 ABC）
- 第 5 期（閑散期）: 採択なし
- 第 6 期（閑散期）: 採択なし
```

第 1 期に罰点を重くして C 社の居座りが短くなると、遅配はほとんどなくなる。
その後は繁忙期に `hold_limit` を延ばす小さな改正が通るだけで、閑散期には
目標を改善する案がない。`block_k 3 → 2` は、どの期も B・C 社の反対で否決される。

## グッドハートの法則（目標を「道路の規則の違反の少なさ」にする）

委員会の目標を、自分の担当である道路の規則の違反件数（期限 `release` の
超過と `blocking_guard` による退去）の少なさにすると、規則は緩める方向に
改正されていく。違反とみなす条件を緩めれば、ロボットの振る舞いが変わらなくても
違反の件数は減るためである。

```bash
python -m town.council --mode DISTRIBUTED --rounds 8 --objective road_violations --name goodhart-d
```

```text
- 第 1 期（平常）: v2 hold_limit 60s → 70s（目標 +2.2，賛成 ABC）
- 第 4 期（繁忙期＋ラッシュ）: v3 block_k 3 → 4（目標 +4.0，賛成 ABC）
- 第 5 期（閑散期）: v4 hold_limit 70s → 80s（目標 +1.0，賛成 ABC）
- 第 7 期（平常）: v5 hold_limit 80s → 90s（目標 +1.4，賛成 ABC）
（ほかの期は採択なし）
```

改正ループが評価に使っていないシードで、バージョンどうしを比べる。

```bash
python -m town.compare runs/goodhart-d/v1.cadl runs/goodhart-d/v5.cadl runs/seasons-d/v4.cadl --mode DISTRIBUTED
```

```text
需要: 注文の平均間隔 75 秒  運用モード DISTRIBUTED  シード [1001, 1002, 1003, 1004, 1005]
  規則                          hold block   λ  定時配達  遅配  道路違反  ふさがれた  集中   A定時率  効用 A / B / C
  runs/goodhart-d/v1.cadl        60     3  0.5    112.2  10.8     25.0      2457s    0%    0.82  36.8 / 39.1 / 19.8
  runs/goodhart-d/v5.cadl        90     4  0.5    106.0  17.0     21.0      3681s    0%    0.72  32.8 / 37.2 / 24.1
  runs/seasons-d/v4.cadl         80     3  1.0    122.8   0.2     19.8       956s    0%    1.00  44.8 / 39.4 / 15.6
```

道路の規則の違反は 25.0 → 21.0 件に減ったが、ふさがれた時間は 1.5 倍、
遅配も 1.6 倍に増え、得をしたのは居座る C 社だけだった。定時配達を目標に
した規則（`seasons-d` の v4）は罰点を重くしているので、道路の規則の違反は
19.8 件と、違反の件数を目標にした v5 より少なく、遅配も 0.2 件になる。
違反の件数を目標にした委員会でも、罰点を重くする案は毎期出ていて、違反の件数を
減らしていた。それでも採られなかったのは、緩める案のほうが件数の減り方が同じか
大きかったためである（第 1 期はどちらも 23.6 件で、先に並ぶ緩める案が採られた）。
罰点を重くする案には、C 社がいつも反対する。

憲法の不変条件（各社の定時率 6 割以上）は、この程度の悪化では破られないので、この改正を止められない。

## 運用モードの切替（分散 ⇄ 集中）

| モード | 待機の場所 | 道路の使い方 |
|---|---|---|
| DISTRIBUTED（分散） | 各社が道路上（中央の交差点 8）で待つ | 交差点を自由に通る |
| CENTRALIZED（集中） | 管理者が待避所（ノード 5・2・7 の脇、各 1 台）を割り当てる。道路上での待機は禁止 | 管理者がエッジを 1 台ずつ専有で割り当てる（C-SoS と同じ）。エッジに入るたびに許可待ち 1 秒 |

集中は交差点をふさがれない反面、注文が非常に多いとエッジの許可待ちが
積み重なる。モードを固定して需要を変えると（シード 1〜5 の平均の定時配達）：

| 注文の平均間隔 | 30 秒 | 45 秒 | 75 秒 | 120 秒 | 180 秒 |
|---|---|---|---|---|---|
| 分散 | **319.6** | 210.2 | 121.8 | 76.8 | 51.2 |
| 集中 | 309.2 | **217.4** | **132.2** | **83.2** | **58.6** |

切り替える条件は CADL に書く。5 分ごとに、直前 5 分の観測で判定する。

```yaml
  transitions:
    - from: DISTRIBUTED
      to: CENTRALIZED
      condition: "orders_in_window < 22"      # @amend:to_central
      protocol: SWITCH
      safety_invariant: "robots_on_road == 0"
    - from: CENTRALIZED
      to: DISTRIBUTED
      condition: "orders_in_window >= 26"     # @amend:to_distributed
      protocol: SWITCH
```

分散 → 集中の切替では、道路上で待っているロボットが全員いなくなるまで
新しいモードを有効にしない（切替中）。C 社は、罰点の倍率が 0.5 以下なら、切替を告げられてから 30 秒たって道路を離れる。
60 秒たっても残っていれば管理者が退去させる。有効にした時点で
`safety_invariant` を判定する。

ラッシュのある期を、改正ループが使っていないシードで比べる。

```bash
python -m town.compare cadl/robot_town.cadl --rush 1200 2400 25 --mode DISTRIBUTED
python -m town.compare cadl/robot_town.cadl --rush 1200 2400 25 --mode CENTRALIZED
python -m town.compare cadl/robot_town.cadl --rush 1200 2400 25 --mode adaptive
```

| モード | 定時配達 | 遅配 | ふさがれた時間 | 集中の時間 |
|---|---|---|---|---|
| 分散に固定 | 202.8 | 8.0 | 2141s | 0% |
| 集中に固定 | 206.2 | 4.6 | 843s | 100% |
| 条件で切替（adaptive） | **209.2** | **1.6** | 1116s | 60% |

切替は 1 期に 3 回（開始 5 分で集中へ、ラッシュで分散へ、ラッシュ後に集中へ）。

道路上の待機が残っていても切替をすぐ有効にすると（`--no-quiescence`）、
安全条件 `robots_on_road == 0` の違反が 1 期あたり平均 1.4 回記録される。

```bash
python -m town.period --rush 1200 2400 25 --no-quiescence
```

改正ループは、既定では切替つき（adaptive）で実行され、切替のしきい値
`to_central` / `to_distributed` も改正の対象になる。

## LLM の委員会（--proposer llm）

改正案を LLM（Claude Opus 5.5）に書かせる。LLM には、その期の観測（会社ごとの
定時率・罰点・効用、原因別の違反）、これまでの改正履歴、憲法、現行の規則を
渡し、改正後の規則ファイル全体を最大 3 案返させる（`town/llm_council.py`）。
LLM が「何を変えたか」をどう申告しても信用せず、返ってきた CADL を、
ルールベースの案と同じ判定にかける。

| 判定 | 条件 | LLM の案で起こりうること |
|---|---|---|
| 検査 | `cadl check` / `cadl sim-ir` を通り、街を動かす値が IR にそろう | 書式の誤り、期限を「1 minute」と書くなど |
| 憲法（文面） | 変えたのが `@amend` 行の数値だけで、1 パラメータ 1 ステップ。委員会が変えてよいのは規則の層だけ | 契約の層（顧客への配達期限）を変える、一度に大きく動かす |
| 憲法（不変条件） | 改正後のシミュレーションで、各社の定時率 6 割以上など | — |
| 改善・投票 | ルールベースの案と同じ | 理由はもっともらしいが、測ると改善していない |

```bash
export ANTHROPIC_API_KEY=...                       # または `ant auth login`
python -m town.council --proposer llm --mode DISTRIBUTED --rounds 2 --name llm-distributed
python -m town.council --proposer llm --check      # LLM を呼ばず、配線だけ確かめる
```

LLM の案は `runs/<名前>/llm-round<k>.json` に保存され、`--proposer replay` で
LLM を呼ばずに再生できる。`examples/llm-replay/llm-round1.json` は、判定の
働きを見せるために手で書いた見本（LLM の出力ではない）。

```bash
python -m town.council --proposer replay --replay-dir examples/llm-replay --mode DISTRIBUTED
```

```text
  [1] block_k 3 → 2：居座りを早めに退去させる
  block_k 3 → 2：居座りを早めに退去させる               131.0  35.2 (34.0)      985s    ○    ○    ○   ○ × ×
  [2] 集荷の期限を 120 秒から 180 秒に延ばす
  集荷の期限を 120 秒から 180 秒に延ばす                     -     -      -          -    ○    ×    -    -     pickup_deadline の行どうしで値が食い違う（120, 180）
  [3] hold_limit を「1 minute」と書き直す
  hold_limit を「1 minute」と書き直す                  -     -      -          -    ×    -    -    -     Type check failed: 1 error(s), 0 warning(s)
  → 採択なし（すべての案がどこかの判定で除かれた）。規則は変わらない。
```

`examples/llm-replay/` の見本には、2026-10-19 に初期の規則の AMEND に加えた 2 つのステップ
（`TOWN_STEWARD -> COUNCIL : violation_report`、`COUNCIL -> TOWN_STEWARD : rule_update`）と
interface の項目、2026-10-29 に `DELIVERY_SLA` に付けた契約の層の印
（`# @amend:pickup_deadline`、`# @amend:dropoff_deadline`）を、機械的に入れてある。憲法の判定は
案を現行の規則と 1 行ずつ比べるためで、各案の `edits` にこのことを記録している。2 件目の案は、
集荷の期限の行だけを 180 秒にしたので、guarantee の行と値が食い違って憲法の判定で止まる。

### 実際の LLM の案（分散モードに固定、2 期）

`examples/llm-distributed/` は、Claude Opus 5.5 が実際に書いた案（2026-11-16、
`--proposer llm --mode DISTRIBUTED --rounds 2`）。LLM を呼ばずに再生できる。

合議の記録には依頼の要約（`request_digest`）が入る。再生のときは、いまの規則・憲法・改正履歴・
プロンプトから依頼を組み立てて記録と比べ、違えば「注意：記録したときと依頼の内容が違う」と
表示する（`town.steward` の記録と同じ仕組み）。手で書いた `examples/llm-replay/` の見本にも、
いまの依頼の要約を入れてある。

```bash
python -m town.council --proposer replay --replay-dir examples/llm-distributed --mode DISTRIBUTED --rounds 2
```

| 期 | LLM の案 | 検査 | 憲法 | 改善 | 投票（A B C） |
|---|---|---|---|---|---|
| 1（平常） | `block_k` 3 → 2 | ○ | ○ | ○ | ○ × × |
| | `hold_limit` 60s → 50s | ○ | ○ | × | ○ × × |
| 2（平常＋ラッシュ） | `hold_limit` 60s → 50s | ○ | ○ | × | ○ ○ × |
| | `block_k` 3 → 2 | ○ | ○ | ○ | ○ × × |

- 4 案とも、検査と憲法（文面）を通る正しい改正だったが、どの期も採択はなかった。
- ルールベースの委員会が第 1 期に採る `sanction_lambda 0.5 → 1` を、LLM は出さなかった。
  LLM には各社の行動方針を渡していないので、罰点の倍率で C 社の行動が変わることを知らない。
- LLM の投票の予想は外れた。第 1 期に LLM は「B 社もふさがれた秒数 849 が減るので賛成する見込み」と
  書いたが、道路で待って得をしている B 社は反対した。LLM は B 社の投票を、B 社自身の利害ではなく
  町全体の利益から予想していた。
- 1 期あたり入力約 5,300、出力約 7,000 トークン。出力の大半は 2 案分の規則ファイル全体。

当初は運用モードを固定していることを LLM に伝えておらず、LLM は「安全条件
`robots_on_road == 0` が満たされないので集中に切り替わらない」という誤った因果を
書いた。依頼に運用モードの設定を含めてからは、「分散に固定されているので
to_central / to_distributed を動かしても結果は変わらない」と正しく読んでいる。

LLM の呼び出しには、安全分類器に断られたとき推奨のモデルで続ける
サーバー側のフォールバック（`fallbacks: "default"`）を付けている。
断られた・上限に達した・出力を読めないときは、その期は LLM の案なしとして続ける。

## 人間が KPI を決め、LLM が提案する（town.steward）

| 担当 | すること | 残るもの |
|---|---|---|
| 人間 | `kpi.md` に KPI を自然言語で書く（`## 第 k 期から` の節で、期ごとに変えてよい）。LLM の判断要求に答え、理由を自然言語（`reason`）か GSN（`gsn:`）で残す | `answers.yaml`、`decisions.md`、`gsn.yaml`（図は `gsn.md`）、`decisions.jsonl` |
| LLM | KPI を評価方法（`town/kpi.py` の指標の組み合わせ）に言い換える。KPI に向けて、規則・規制・契約の改正案を出す。決めきれないことを判断要求にまとめる（`town/llm_steward.py`） | `llm/p<k>-kpi.json`、`p<k>-propose.json`、`p<k>-request.json` |
| 機械による判定 | 検査・憲法・同じシードで測った改善・投票（`town.council` と同じ）。LLM の申告は信用しない | `p<k>-evidence.md`、`v<k>.cadl`、`constitution-v<k>.yaml` |

改正の層は 3 つ（`constitution.yaml` の `layer` と `regulation`）。

| 層 | 対象 | 採否 |
|---|---|---|
| 規則 | `ROAD_USE`、運用モードの切替 | 判定をすべて通れば自動で採択できる。切替のしきい値（`safety: true`）は人間が決める |
| 契約 | `DELIVERY_SLA`（集荷と配達の約束の秒数） | 顧客は投票に加わらないので、人間が決める |
| 規制 | 憲法の不変条件と定足数 | 規制の担当者（運営者とは別の人間）が、別の判断要求 `R<k>` で決める |

次のどれかに当たる案があると、自動では採択せず、LLM が判断要求を書いて人間の答えを待つ（`town/steward.py` の `triage()`）。

| 理由 | 当たるとき |
|---|---|
| 合意 | KPI はよくなるのに投票で否決され、自動で採択できる案より KPI がよい |
| 契約・規制・安全 | 契約の層、規制、運用モードの切替を変える（契約と安全は、KPI がよくなり不変条件を守るときだけ。KPI がよくならない契約の案は、LLM が判断を求めたときだけ判断要求に入る） |
| KPI の衝突 | 主目標はよくなるが、KPI の条件を破るか、監視している目標が悪化する |
| 不確か | 判定はすべて通るが、改善したシードが 8 割に満たない |
| LLM | LLM 自身が、人間の判断が要ると書いた（憲法の不変条件を守る案に限る） |

KPI が変わったときも、読み方が分かれる・代理指標の恐れがある・過去の判断の前提が崩れる、
のどれかなら、LLM が選択肢を添えて人間に確かめる。`--lang en` で、人間に見せる文言と LLM が
書く文章が英語になる（KPI は `kpi.en.md`、見出しは `## From period k`）。

### 評価方法と達成基準を人間が書く

自然言語の KPI だけでは、何を測り、どこまで行けば達成なのかが決まらない。`kpi.md` の節に
「評価方法:」の箇条書きを書くと、`town.steward` はそれをそのまま評価方法として使い、LLM に
読み方を頼まない（見本は `examples/kpi-written/kpi.md`）。

```markdown
## 第 1 期から

顧客に約束した時間のうちに届く配達を、町全体で 95% 以上にしたい。どの会社でも、
約束の合計の 240 秒を超えて待つ顧客を 5% 以下にしたい。

評価方法:
- 主目標: 定時率 >= 0.95
- 監視: 最も待たせた会社の待ち時間p95 <= 240
- 条件: 最低の会社の定時率 >= 0.8
```

| 行 | 意味 |
|---|---|
| `主目標` | 1 つだけ。改善はこれで測る。値を書けば達成基準になる（書かなければ、よくする向きだけを決める） |
| `監視` | 悪化すれば KPI の衝突として人間に判断を求める。値を書けば達成基準になる |
| `条件` | 値が必須。改正後に必ず守る（破る案は KPI の衝突になる） |

- 指標は、名前（`on_time_rate`）でも日本語の短い名前（`定時率`）でも書ける。`95%` とも書ける。
  向きは指標で決まり、大きいほどよい指標は `>=`、小さいほどよい指標は `<=` で書く（逆なら
  エラーで止まる）。英語では `Evaluation:`、`- goal:`、`- watch:`、`- guard:`。
- 待ち時間は、注文から届くまでの秒数。期の終わりに届いていない注文は、期の終わりまでの秒数で
  数える。`待ち時間p95`（町全体）、`最長の待ち時間`、`最も待たせた会社の待ち時間p95` がある。
- 期ごとに、現行の規則で達成基準と条件に届いているかを、端末、`decisions.md`、`p<k>-evidence.md` に
  「KPI の達成状況」として示す。改正案ごとの表（`p<k>-evidence.md` と判断要求）にも、
  改正後に達成基準と条件に届くかを添える。達成基準は表示のためのもので、改正を採るかどうかの判定には使わない。
- 箇条書きのない節は、これまでどおり LLM が評価方法に言い換える。LLM も達成基準（`target`）を
  書けるが、KPI の文に書かれていない水準を決めてはいけないと依頼している。

使える指標の一覧と、`kpi.md` の各節をどう読むかは、次で確かめられる。

```bash
python -m town.kpi                              # kpi.md
python -m town.kpi examples/kpi-written/kpi.md
```

```bash
python -m town.steward --mode DISTRIBUTED --rounds 6 --name kpi-d             # LLM を呼ぶ。判断要求には端末で答える
python -m town.steward --mode DISTRIBUTED --rounds 6 --name kpi-d \
    --llm replay --replay-dir examples/steward-d --answers examples/steward-d/answers.yaml
python -m town.steward --lang en --mode DISTRIBUTED --rounds 6 --name kpi-d-en \
    --llm replay --replay-dir examples/steward-d-en --answers examples/steward-d-en/answers.yaml
```

端末がなく、`--answers` にも答えがないときは、判断要求を `runs/<名前>/p<k>-request.md` に
書き出して止まる。答えを YAML に書き、同じ `--name` のまま `--answers <YAML>` を足して同じ
コマンドを実行すると、`runs/<名前>/llm/` に記録のある LLM の出力は呼び直さずに続く。
記録には依頼の要約（`request_digest`）が入っており、再生のときに依頼が記録と違えば
（`kpi.md`、規則、これまでの判断が変わったなど）、「注意：記録したときと依頼の内容が違う」と出る。

### 実際の LLM の記録（examples/steward-d、6 期）

`examples/` の次の記録は、2026-11-18 に Claude Opus 5.5 で実際に実行したもの（cadl 0.3.36）。
判断要求への答え（`answers.yaml`）は、記録を作るときに人間（運営者）の役を Claude が務めて書いた。

| 記録 | 内容 |
|---|---|
| `steward-d/` | `kpi.md`（日本語）で 6 期。入力約 63,300・出力約 50,100 トークン |
| `steward-d-en/` | `kpi.en.md`（英語）で 6 期。入力約 61,600・出力約 51,100 トークン |
| `steward-goodhart/proxy/`、`guarded/` | 代理指標の KPI（`kpi.md`）に、件数だけを主目標にする答えと、配達の成績・待ち時間・ふさがれた時間も監視する答えで 1 期ずつ |
| `steward-goodhart/en/` | 同じ KPI を英語で（`kpi.en.md`）1 期 |

| 期 | 判断を求めた理由（日本語の記録） | LLM の推奨 | 人間の選択 | 結果 |
|---|---|---|---|---|
| 1（平常） | 合意（`block_k 3 → 2` を B・C 社が否決。改善したシードは 5/5） | 1 | `block_k 3 → 2`（否決を覆す） | v2 |
| 2（平常＋ラッシュ） | 不確か（`block_k 2 → 1`、2/5） | keep | 変えない | v2 |
| 3（繁忙期） | なし（`hold_limit 60s → 50s` は 2/5 で、自動で変えない） | — | — | v2 |
| 4（繁忙期＋ラッシュ） | なし（LLM は案を出さなかった） | — | — | v2 |
| 5（閑散期、KPI が変わる） | 合意と KPI の衝突（`block_k 2 → 3`、`hold_limit 60s → 70s`、`sanction_lambda 0.5 → 0`） | keep | 変えない | v2 |
| 6（閑散期） | なし（LLM は案を出さなかった） | — | — | v2 |

- KPI の読み方（K1）では、日本語・英語のどちらの LLM も、「ひどく待たされる」の水準が KPI に
  書かれていないことを挙げ、達成基準（`target`）を置かなかった。英語の LLM は、約束の 2 区間を
  足した 240 秒を水準にする選択肢も出したが、人間が確かめるべき読み方だと書いた。人間は、
  水準を置かずに会社ごとの待ち時間の 95 パーセンタイルを監視する読み方を選んだ。
- 第 1 期は、日本語と英語で同じ結果になった。どちらの LLM も罰点を重くする案を出さず、
  `block_k 3 → 2` を出した。定時配達が 5 つのシードすべてで増え、最も待たせた会社の待ち時間の
  95 パーセンタイルが 273 秒から 95.6 秒に下がる。B・C 社が否決したが、人間は否決を覆して採った。
  LLM の推奨は、日本語では採択、英語では「変えない」（C 社の効用が約 3 分の 1 下がる理由が
  わかっていないため）だった。
- 日本語の第 2 期に、LLM は A・B 社の賛成で定足数に届いている案を「投票の結果を覆す」と書いた。
  判断要求の依頼に憲法を渡していないので、LLM は定足数を知らない。
- 第 5 期は、言語で結論が分かれた。どちらも `hold_limit 60s → 70s` で効用の差を +1.7 縮められる。
  日本語の人間は、K5 で最も待たせた会社の待ち時間の 95 パーセンタイルも監視する読み方を選んでおり、
  この案ではそれが 178 秒から 212 秒に延びると示されたので、変えなかった。英語の読み方は
  この指標を監視しておらず、悪化として示されなかった。LLM はこの案を勧め、人間はこれを採って
  v3 になった。英語の第 6 期の 2 案は、どちらも最も低い会社の定時率を下げるので、変えなかった。
- 罰点をなくす案（`sanction_lambda 0.5 → 0`）と `block_k` を 3 に戻す案は、効用の差を大きく
  縮めるが、遅配が増え、最も低い会社の定時率が下がる。LLM はどちらの言語でも、これを勧めなかった。
  罰点をなくす案について、LLM は「道路の使い方は変わらないまま、効用の評価方法の上でだけ差が消えます」と書いた。
- 代理指標の KPI では、日本語の LLM は罰点を重くする案と `hold_limit 60s → 70s` を出した。どちらも
  違反の件数を 25.8 から 23.6 に減らすが、`hold_limit` の案はふさがれた時間をほとんど減らさない。
  件数だけを主目標にした答え（proxy）でも、LLM は「違反とみなす基準そのものを緩めて違反件数を減らします」と
  書いてこの案について人間に判断を求め、罰点を重くする案を勧めた。配達の成績も監視する答え（guarded）と英語では、
  LLM は罰点を重くする案と `block_k 3 → 2` を出し、罰点を重くする案が自動で採択された。

### 判断の記録（decisions.jsonl）

`town.steward` と `town.council` は、実行ごとに `runs/<名前>/decisions.jsonl` を書き出す。
CADL 仕様の[付録 L](https://www.ertl.jp/cadl-spec/ja/docs/spec/appendix-l-decisions)の形式（decisions 0.3）で、
誰が何を提案し、何を測り、各社がどう投票し、誰が何を推奨し、誰がどの理由で判断したかを、
1 行に 1 項目ずつ残す。各行には、付録 G の G.8 と同じハッシュの連鎖が付く。

```bash
cadl decisions-verify runs/<名前>/decisions.jsonl     # cadl 0.3.39 以降
```

- 規則の各バージョンは `runs/<名前>/v<n>.cadl` に残し、記録の version の項目に添付する（最初の
  バージョン `v1` も）。このファイルは、規則に、[付録 K](https://www.ertl.jp/cadl-spec/ja/docs/spec/appendix-k-amendment)
  の改正の宣言をツールが書き足したもの（`town/model.py`）。`sos.version` を `v<n>` に、運営者と
  規制の担当者を actor に、規制の値を `context.environment` に、`amendment:` に `previous`、
  `effective`、層ごとに誰が決めるか（規則の層は運営者が、憲法の定足数での投票を経て決める。契約の
  層は運営者、規制の層は規制の担当者）と、改正してよいパラメータ（値、範囲、刻み、場所）を書く。
  LLM が読み書きする規則は、これまでどおり `cadl/robot_town.cadl` の形。検証器はこのファイルで、
  バージョンとその順序（L-9）、案が現行のバージョンのパラメータを範囲と刻みの中で動かしているか
  （L-10）、誰が決めたかと投票の結果（L-11、L-6）を確かめる。規制を変えると、モデルの新しい
  バージョンもできる。

- 人間でも LLM でもツールでも、提案・推奨・判断には同じ証拠（理由、前提、分からないこと、予測）を
  残す。人間の答え（`answers.yaml`）に理由がないと、`town.steward` は止まる。答えには
  `unknowns`（分からないこと）、`follow_ups`（後で確かめること）、`fulfils`（前に約束したことを
  果たした項目の id。例 `D1.f1`）も書ける。
- 誰が何を判断するか：運営者は規則と契約（と KPI の読み方）について、運営者とは別の人間である規制の
  担当者は規制（憲法の `regulation:` に挙がった値）について判断する。規制の案は、同じ期の規則と
  契約の判断の後に、別の判断要求（`R<k>`、`p<k>-reg-request.md`。答えは `answers.yaml` の `R<k>`）で
  規制の担当者に出す。規制の変更は次の期から効く（`constitution-v<n>.yaml` と、モデルの新しいバージョン）。記録では、
  判断する当事者が判断してよい層（`layers`）を持つ。判定をすべて通った規則の変更を自分で採択する
  steward（と council）は、運営者に代わって（`on_behalf_of`）`["rule"]` について判断する。
- 誰が誰に判断を任せたか：記録の初めに、運営者は自分に代わって判断する当事者ごとに委任を書く
  （`delegation` の項目 `G.<当事者>`、decisions 0.3）。steward と council には、自分の規則で決める
  こと（`procedures: ["automatic"]`）だけを規則の層について任せ、その規則と判定を `conditions` に
  書く。`--decider llm` では、`--delegate` で与えた層を LLM に任せる。規制の担当者も同じく
  `llm-regulator` に任せる。ツールを動かすこと、`--delegate` を与えることが、人間のその指示である。
  検証器は、委任の範囲で行った判断をその委任のもとに示し、範囲の外の判断にだけ指摘 delegated を出す。
- 規制の案は、判断の前に測る。steward は、案の値を、その期に測った現行の規則と案、その投票に
  当てはめ（新しい実行はしない）、不変条件を満たす案と投票を通る案の数を、その期について、また記録の
  それまでの期に測ったすべての案について（`…_all`）記録する。会社ごとには、その会社が反対したのに
  投票を通る案の数（`passing_against`）を記録し、現行の規則での各社と顧客の値とともに残す
  （`P<k>.R<i>.m`）。
- 約束は、何がそれを果たすかを持ち、街の中の誰かが行うことだけを約束する。答えの `follow_ups` の
  項目は、`text`、`due`（期）、`kind` を持つ。
  - `remeasure`（`metrics` も。例 `utility@C`、`road_violations`）：steward が、次の期の初めに、
    判断した期と同じ条件（同じ乱数シードと需要）で現行の規則を測り直し、`F<k>.<約束の id>` として
    記録する。この測定が約束を果たす（`remeasure: true` もこの種類として読む）。
  - `propose`：改正案を測る約束。改正案を出す LLM が後の期にまだ果たされていない約束を読み、それを
    実行する案が `fulfils` で挙げると果たされる。
  - `ask`（既定）：後の判断で決める問い。後の判断が `fulfils` で挙げると果たされる。期限の期が
    来ても決まっていなければ、steward はそれを約束した側の判断する者に、別の判断要求で渡す
    （運営者は `Q<k>`、規制の担当者は `QR<k>`。`p<k>-ask.md`。答えは `answers.yaml` の `Q<k>` に、
    `keep` と理由で書く）。最後の期には、残った問いをすべて渡す。最後の期の判断要求には、その期に
    した約束は果たされないと書く。
  種類の違う約束を判断が `fulfils` で挙げても約束は果たされず、検証器は fulfilment not shown を報告する。
- 判断が改正案を採択すると、steward は、判断に使わなかった、シナリオの次の期（最後の期のあとは最初の期）のシードと需要で、採択した案とそれまでの規則
  を測る（案の `D<k>.n` を、有効な規則の `D<k>.n0` と比べる）。検証器は、判断、その推奨、案の予測を、選ぶの
  に使わなかった測定で確かめ、採択した変更が持ちこたえなければ prediction missed を報告する。
  `cadl decisions-verify`（0.3.41 以降）は、確かめた予測の数と、確かめられなかった理由（`keep` について
  の予測、採択しなかった選択肢についての予測で、誰も測り直さないもの）を数える。
- `--decider llm` では、`--delegate` で与えた層（既定は `rule,contract`。`rule` と `contract` は同じ
  判断要求で扱うので、両方を任せるか、どちらも任せない）について、人間に代わって LLM が判断要求に
  答える。運営者に代わるときは `llm-decider`（`llm/p<k>-decide.json`、`llm/p<k>-kpi-decide.json`）、
  規制の担当者に代わるときは `llm-regulator`（`llm/p<k>-reg-decide.json`）として記録される。任せない
  層の判断要求は人間に出す。LLM は、まだ果たされていない約束を読み、判断で決める問いを `fulfils` に
  挙げ、自分の約束に種類（`remeasure`、`propose`、`ask`）を付ける。街の中で誰も行わない調べものは
  約束せず、まだ果たされていない約束を出し直さないよう指示している。LLM の判断は、委任
  `G.llm-decider`（または `G.llm-regulator`）のもとの判断になる。
- `--layers rule,contract` で、改正案を規則と契約の層に限る（規制の案は測らずに除く）。
- 見本（Claude Opus 5.5 が人間に代わって判断した、2026-11-22 の記録）：`examples/steward-contract/`
  （日本語）と `examples/steward-contract-en/`（英語）は、契約（約束の時間）の見直しを許す KPI での
  6 期（`--layers rule,contract`）。`examples/steward-regulation/` と `examples/steward-regulation-en/`
  は、規制の見直しを許す KPI での 6 期（`--delegate rule,contract,regulation`）。

## 見本を確かめる

`python -m town.check_examples` は、すべての見本を、記録した LLM の出力を再生してもう一度実行し
（API キーは要らない）、改正の履歴、GSN、判断の記録の指摘を `examples/expected/<名前>/` と比べる。
記録が検証でエラーにならないことと、LLM への依頼が記録したときと同じであることも確かめる。
`--update` で今の結果をそこに書く。GitHub Actions は、PR と main への push のたびに、Python 3.9 と
3.14 でこれを実行する。

## 制約と、まだないもの

- 罰点の倍率（`incentive` の `lambda`）は CADL の検査で 0〜1 に限られ、改正では 0、0.5、1.0 の
  どれかになる。各社の罰点への反応（`town/policies.py`）は、この 3 段階で決めている。
- 一度採択した改正は、環境が戻っても自動では戻らない（廃止の提案は、
  隣の案として次の期に評価されるだけ）。
- 目標を `--objective violations`（配達の遅れも含む違反全体）にすると、
  規則は緩める方向に改正されない。遅配も違反として数えるので、規則を緩めて遅配が増えると目標が悪くなるため。
- LLM の所見や理由の中の事実の誤り（例：A 社の遅配がすべて集荷の遅れだと
  断定する。観測からはそこまで言えない）は、判定では検査していない。
- Unity・実機での再生はまだない。
- CADL 仕様の課題として見つかったもの：
  - `cadl` 0.3.31 までの `sim-ir` は `sos.transitions[*].safety_invariant` を IR に
    出力しなかった（0.3.32 から出力する）。`town/period.py` は、ソースに書かれた安全条件が
    IR にそろわない規則を却下する。
  - 0.3.32 から、運用モードごとに罰点の倍率などの値と、効力のある契約を結びつける
    `sos.regimes` が書ける。この街はまだその束縛を当てはめないので、`sos.regimes` のある規則は
    無視せずに却下する。また、集中モードで変わること（待避所、エッジの専有）は値では
    なく振る舞いなので、シミュレータ側（`town/world.py`）が決めている。
  - `cadl` 0.3.11 までは、`deadline: soon` のような誤った期限も `cadl check` を通り、
    IR では `null` になっていた（0.3.12 からはエラー）。`town/period.py` は、古い
    コンパイラでも、IR に値がそろわない規則を却下する。
- `town/contract_engine.py` は cadl-raspimouse-simulator の
  `cadl/runtime/engine.py`（commit 3fdf7d3）の複製。

## ライセンス

Apache License 2.0（[LICENSE](LICENSE)、[NOTICE](NOTICE)）。
