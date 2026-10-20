# ロボットの街：規則を自分たちで直す（コース D）

[English](README.md) | 日本語

教材ページ：[コース D — ロボットの街](https://www.ertl.jp/cadl-spec/ja/docs/handson/robot-town-tutorial)

3 社の配送ロボット（各社 2 台）が、ラズパイマウスのシミュレータと同じ
11 ノードの道路網を共有する。道路のルールは CADL（`cadl/robot_town.cadl`）
に書かれ、委員会がシミュレーション結果を見て改正していく。

- 規範の判定（期限・監視・状態遷移）は、コース A と同じ Python 参照
  ランタイム（`town/contract_engine.py`）が IR を読んで行う。
- 走っているロボットは交差点を通り抜けられる。交差点に止まっている
  ロボット（積み下ろし中・道路上で待機中）がいると、誰も入れない。
- 各社の行動方針は `town/policies.py`。A 社は協調型、B 社は期限ぎりぎり
  まで道路で待つ計算型、C 社は罰則が重くなるまで居座る強欲型。
- 運用モードは分散（DISTRIBUTED）と集中（CENTRALIZED）の 2 つ。切り替える
  条件は CADL の `sos.transitions` に書かれる（後述）。

## セットアップ

コース A のセットアップ（`~/program/cadl_repo` に CADL コンパイラをクローン）が
済んでいることが前提。手順とインストールの問題への対処は
[テキストブックの 2.4 節](https://www.ertl.jp/cadl-spec/ja/docs/textbook/toolchain-setup/#24-セットアップ-5-分)にある。

```bash
cd ~/program
git clone https://github.com/ertlnagoya/cadl-robot-town
cd cadl-robot-town
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip   # macOS の python3（3.9）の pip は古く、次の行が失敗する
pip install -e ~/program/cadl_repo     # cadl コマンドと PyYAML が入る
pip install anthropic                  # LLM の委員会（--proposer llm）を使うときだけ
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

## 改正ループを回す

```bash
python -m town.council --mode DISTRIBUTED            # 1 期
python -m town.council --mode DISTRIBUTED --rounds 6 # 6 期
```

改正案は `constitution.yaml` の `amendable` にあるパラメータ（CADL の
`# @amend:<名前>` 行）を 1 つだけ 1 ステップ動かしたもの。検査・憲法・
改善・投票の 4 つの関門を通った案のうち、目標が最もよくなる案を採択する。

```text
=== 第 1 期（平常：注文の平均間隔 75 秒）  現行 v1: sanction_lambda=0.5, hold_limit=60, block_k=3, to_central=22, to_distributed=26 ===
  改正案                                  定時配達  違反 (道路) ふさがれた  検査 憲法 改善 投票(A B C)
  hold_limit 60s → 50s                     120.6  40.4 (28.8)     2468s    ○    ○    ×   ○ × ×
  hold_limit 60s → 70s                     124.2  31.6 (23.6)     2367s    ○    ○    ○   ○ ○ ○
  block_k 3 → 2                            131.0  35.2 (34.0)      985s    ○    ○    ○   ○ × ×
  ...
  → 採択: hold_limit 60s → 70s（目標 +2.4，賛成 ABC）。v2.cadl として保存。
```

町全体に一番よい案（`block_k 3 → 2`：待たされる台数 2 で退去）は、
居座りで得をしている B 社・C 社の反対で否決され、全社が賛成する小さな
改正が通る。

## 需要の変化（scenario.yaml）

第 k 期の需要は `scenario.yaml` の `periods[k-1]` で決まる。平常・繁忙期・
閑散期があり、平常と繁忙期の 2 期目には、期の途中（20〜40 分）に注文が
集中するラッシュがある。期ごとに乱数シードも変わり、現行の規則もその期の
環境で評価し直してから改正案と比べる。

```bash
python -m town.period --interval 45                 # 繁忙期だけを 1 期
python -m town.period --rush 1200 2400 25           # 平常＋ラッシュを 1 期
```

分散モードに固定して 6 期回すと（`--mode DISTRIBUTED --rounds 6 --name seasons-d`）、
次のようになる。

```text
- 第 1 期（平常）: v2 hold_limit 60s → 70s（目標 +2.4，賛成 ABC）
- 第 2 期（平常＋ラッシュ）: v3 hold_limit 70s → 80s（目標 +1.6，賛成 ABC）
- 第 3 期（繁忙期）: v4 hold_limit 80s → 70s（目標 +6.0，賛成 ABC）
- 第 4 期（繁忙期＋ラッシュ）: v5 hold_limit 70s → 60s（目標 +4.0，賛成 ABC）
- 第 5 期（閑散期）: v6 block_k 3 → 2（目標 +6.0，賛成 AB）
- 第 6 期（閑散期）: v7 block_k 2 → 1（目標 +2.0，賛成 AB）
```

平常期と繁忙期には B 社・C 社の反対で否決され続けた `block_k 3 → 2` が、
閑散期に B 社が賛成に回って通る。注文が少ないと、道路で待って次の注文に
早く着く利得より、他社に待たされる損のほうが B 社にとって大きくなるため。

## グッドハート型の暴走（目標を「道路の規則の違反の少なさ」にする）

委員会の目標を、自分の担当である道路の規則の違反件数（期限 `release` の
超過と `blocking_guard` による退去）の少なさにすると、規則は緩む方向に
流れていく。違反を減らす一番簡単な方法は、違反と数えないことだから。

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

改正ループが評価に使っていないシードで、版どうしを比べる。

```bash
python -m town.compare runs/goodhart-d/v1.cadl runs/goodhart-d/v5.cadl runs/seasons-d/v7.cadl --mode DISTRIBUTED
```

```text
需要: 注文の平均間隔 75 秒  運用モード DISTRIBUTED  シード [1001, 1002, 1003, 1004, 1005]
  規則                          hold block   λ  定時配達  遅配  道路違反  ふさがれた  集中   A定時率  効用 A / B / C
  runs/goodhart-d/v1.cadl        60     3  0.5    112.2  10.8     25.0      2457s    0%    0.82  36.8 / 39.1 / 19.8
  runs/goodhart-d/v5.cadl        90     4  0.5    106.0  17.0     21.0      3681s    0%    0.72  32.8 / 37.2 / 24.1
  runs/seasons-d/v7.cadl         60     1  0.5    123.0   0.0     28.2        52s    0%    1.00  45.0 / 41.8 / 3.7
```

道路の規則の違反は 25.0 → 21.0 件に減ったが、ふさがれた時間は 1.5 倍、
遅配も 1.6 倍に増え、得をしたのは居座る C 社だけだった。定時配達を目標に
した規則（v7）は、道路の規則の違反がむしろ多いのに、遅配はゼロになる。
数えている違反の少なさと、街の良さは別物である。

憲法の不変条件（各社の定時率 6 割以上）は、この程度の悪化では働かない。

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
新しいモードを有効にしない（切替中）。C 社は罰則が軽いうちは 30 秒渋る。
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

改正ループは既定で切替つき（adaptive）で回り、切替のしきい値
`to_central` / `to_distributed` も改正の対象になる。

## LLM の委員会（--proposer llm）

改正案を LLM（Claude Opus 5.5）に書かせる。LLM には、その期の観測（会社ごとの
定時率・罰点・効用、原因別の違反）、これまでの改正履歴、憲法、現行の規則を
渡し、改正後の規則ファイル全体を最大 3 案返させる（`town/llm_council.py`）。
LLM が「何を変えたか」をどう申告しても信用せず、返ってきた CADL を、
ルールベースの案と同じ関門にかける。

| 関門 | 判定 | LLM の案で起こりうること |
|---|---|---|
| 検査 | `cadl check` / `cadl sim-ir` を通り、街を動かす値が IR にそろう | 書式の誤り、期限を「1 minute」と書くなど |
| 憲法（文面） | 変えたのが `@amend` 行の数値だけで、1 パラメータ 1 ステップ | 改正の対象外（顧客への配達期限など）に手を出す、一度に大きく動かす |
| 憲法（不変条件） | 改正後のシミュレーションで、各社の定時率 6 割以上など | — |
| 改善・投票 | ルールベースの案と同じ | もっともらしい理由でも、数字が伴わない |

```bash
export ANTHROPIC_API_KEY=...                       # または `ant auth login`
python -m town.council --proposer llm --rounds 3
python -m town.council --proposer llm --check      # LLM を呼ばず、配線だけ確かめる
```

LLM の案は `runs/<名前>/llm-round<k>.json` に保存され、`--proposer replay` で
LLM を呼ばずに再生できる。`examples/llm-replay/llm-round1.json` は、関門の
働きを見せるために手で書いた見本（LLM の出力ではない）。

```bash
python -m town.council --proposer replay --replay-dir examples/llm-replay --mode DISTRIBUTED
```

```text
  [1] block_k 3 → 2：居座りを早めに退去させる
  block_k 3 → 2：居座りを早めに退去させる               131.0  35.2 (34.0)      985s    ○    ○    ○   ○ × ×
  [2] 集荷の期限を 120 秒から 180 秒に延ばす
  集荷の期限を 120 秒から 180 秒に延ばす                     -     -      -          -    ○    ×    -    -     @amend 行の数値以外を書き換えている
  [3] hold_limit を「1 minute」と書き直す
  hold_limit を「1 minute」と書き直す                  -     -      -          -    ×    -    -    -     Type check failed: 1 error(s), 0 warning(s)
  → 採択なし（すべての案がどこかの関門で止まった）。規則は変わらない。
```

### 実際の LLM の案（分散モードに固定、2 期）

`examples/llm-distributed/` は、Claude Opus 5.5 が実際に書いた案（2026-10-15、
`--proposer llm --mode DISTRIBUTED --rounds 2`）。LLM を呼ばずに再生できる。

2026-10-19 に、初期の規則の AMEND に 2 つのステップ（`TOWN_STEWARD -> COUNCIL :
violation_report`、`COUNCIL -> TOWN_STEWARD : rule_update`）と、それに合わせた
interface の項目を加えた。憲法の関門は案を現行の規則と 1 行ずつ比べるので、
`examples/llm-distributed/` と `examples/llm-replay/` の記録済みの案すべてに、
同じ行を機械的に入れてある。案のほかの部分は変えておらず、再生の結果も変わらない。
各ファイルの `edits` にこのことを記録している。

```bash
python -m town.council --proposer replay --replay-dir examples/llm-distributed --mode DISTRIBUTED --rounds 2
```

| 期 | LLM の案 | 検査 | 憲法 | 改善 | 投票（A B C） |
|---|---|---|---|---|---|
| 1（平常） | `block_k` 3 → 2 | ○ | ○ | ○ | ○ × × |
| | `hold_limit` 60s → 50s | ○ | ○ | × | ○ × × |
| | `sanction_lambda` 0.5 → 1.0 | ○ | ○ | × | ○ × × |
| 2（平常＋ラッシュ） | `block_k` 3 → 2 | ○ | ○ | ○ | ○ × × |
| | `hold_limit` 60s → 50s | ○ | ○ | × | ○ ○ × |
| | `sanction_lambda` 0.5 → 1.0 | ○ | ○ | × | ○ × × |

- 6 案とも、検査と憲法（文面）を通る正しい改正だった。
- 第一案は両期とも、町全体に一番よい `block_k 3 → 2`（ルールベースの探索と同じ）。
- 投票の読みは外れた。LLM は「B 社も賛成し定足数に届く」と見込んだが、道路で
  待って得をしている B 社は反対した。町全体の利益で B 社の投票を予想している。
- 全社が賛成する「緩める」案（ルールベースの委員会が第 1 期に採択した
  `hold_limit 60s → 70s`）は出さなかった。3 案とも厳しくする方向だった。
- `sanction_lambda` を上げる案は効かなかった。C 社が行動を変えるのは倍率 1.5
  以上で、1 回に動かせるのは 0.5 まで。LLM には各社の行動方針を渡していない。
- 1 期あたり入力約 4,400、出力約 9,300 トークン（約 20 セント）。出力の大半は
  3 案分の規則ファイル全体。

当初は運用モードを固定していることを LLM に伝えておらず、LLM は「安全条件
`robots_on_road == 0` が満たされないので集中に切り替わらない」という誤った因果を
書いた。依頼に運用モードの設定を含めてからは、「分散に固定されているので
to_central / to_distributed を動かしても結果は変わらない」と正しく読んでいる。

LLM の呼び出しには、安全分類器に断られたとき推奨のモデルで続ける
サーバー側のフォールバック（`fallbacks: "default"`）を付けている。
断られた・上限に達した・出力を読めないときは、その期は LLM の案なしとして続ける。

## 制約と、まだないもの

- 一度採択した改正は、環境が戻っても自動では戻らない（廃止の提案は、
  隣の案として次の期に評価されるだけ）。
- 目標を `--objective violations`（配達の遅れも含む違反全体）にすると、
  暴走は起きない。遅配が違反として数えられ、目標が街の良さとずれないため。
- LLM の見立てや理由の中の事実の誤り（例：A 社の遅配がすべて集荷の遅れだと
  断定する。観測からはそこまで言えない）は、関門では検査していない。
- Unity・実機での再生はまだない。
- CADL 仕様の課題として見つかったもの：
  - `cadl` 0.3.18 の `sim-ir` も `sos.transitions[*].safety_invariant` を IR に
    出力しない。`town/period.py` は検査を通ったソースから読んで添えている。
  - 運用モードに規則のパラメータ値を結びつける構文がない。集中モードで何が
    変わるか（待避所、エッジの専有）は、シミュレータ側（`town/world.py`）が決めている。
  - `cadl` 0.3.11 までは、`deadline: soon` のような誤った期限も `cadl check` を通り、
    IR では `null` になっていた（0.3.12 からはエラー）。`town/period.py` は、古い
    コンパイラでも、IR に値がそろわない規則を却下する。
- `town/contract_engine.py` は cadl-raspimouse-simulator の
  `cadl/runtime/engine.py`（commit 3fdf7d3）の複製。

## ライセンス

Apache License 2.0（[LICENSE](LICENSE)、[NOTICE](NOTICE)）。
