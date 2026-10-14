# ロボットの街：規則を自分たちで直す（コース D 試作）

3 社の配送ロボット（各社 2 台）が、ラズパイマウスのシミュレータと同じ
11 ノードの道路網を共有する。道路のルールは CADL（`cadl/robot_town.cadl`）
に書かれ、委員会がシミュレーション結果を見て改正していく。

- 規範の判定（期限・監視・状態遷移）は、コース A と同じ Python 参照
  ランタイム（`town/contract_engine.py`）が IR を読んで行う。
- 走っているロボットは交差点を通り抜けられる。交差点に止まっている
  ロボット（積み下ろし中・道路上で待機中）がいると、誰も入れない。
- 各社の行動方針は `town/policies.py`。A 社は協調型、B 社は期限ぎりぎり
  まで道路で待つ計算型、C 社は罰則が重くなるまで居座る強欲型。

## セットアップ

```bash
cd ~/program/cadl/robot-town-exercise
python3 -m venv .venv
source .venv/bin/activate
pip install -e ~/program/cadl_repo     # cadl コマンドと PyYAML が入る
```

## 1 期だけ動かす

```bash
python -m town.period
```

```text
規則: robot_town.cadl  (hold_limit=60s, block_k=3, sanction_lambda=0.5)
  定時配達 121.8 / 注文 132.2  未配達 0.0  ふさがれた時間 2404s  （シード平均）
  会社  定時率  遅配  道路で待った回数  ふさがれた秒数  罰点   効用
   A     0.84   7.8               0.0            1352    0.0   38.4
   B     0.94   2.6              38.0             849    0.9   38.9
   C     1.00   0.0              39.6             203   14.8   28.8
  違反（原因別，シード平均）: deadline:dropoff=2.6, deadline:pickup=7.8, deadline:release=23.0, monitor:blocking_guard=2.8
```

C 社は交差点に居座って定時率 100% を得て、そのぶん A 社がふさがれる。

## 改正ループを回す

```bash
python -m town.council                 # 1 期
python -m town.council --rounds 5      # 5 期
```

改正案は `constitution.yaml` の `amendable` にあるパラメータ（CADL の
`# @amend:<名前>` 行）を 1 つだけ 1 ステップ動かしたもの。検査・憲法・
改善・投票の 4 つの関門を通った案のうち、目標が最もよくなる案を採択する。

```text
=== 第 1 期  現行 v1: sanction_lambda=0.5, hold_limit=60, block_k=3 ===
  改正案                              定時配達  違反   検査 憲法 改善 投票(A B C)
  hold_limit 60s → 50s                 120.6   40.4    ○    ○    ×   ○ × ×
  hold_limit 60s → 70s                 124.2   31.6    ○    ○    ○   ○ ○ ○
  block_k 3 → 2                        131.0   35.2    ○    ○    ○   ○ × ×
  ...
  → 採択: hold_limit 60s → 70s（目標 +2.4，賛成 ABC）。v2.cadl として保存。
```

町全体に一番よい案（`block_k 3 → 2`：待たされる台数 2 で退去）は、
居座りで得をしている B 社・C 社の反対で否決され、全社が賛成する小さな
改正が通る。第 2 期以降は、どの案もいずれかの関門で止まり、規則は
v2 のまま動かなくなる。

採択された版は `runs/<名前>/v1.cadl, v2.cadl, …` と `history.md` に残る。
cadl-explorer の Designer で版どうしを比較できる。

## 試作の範囲と、まだないもの

- 毎期、同じ乱数シード・同じ需要で回すので、改正が止まると以後も変わらない。
  期ごとの需要の変化（繁忙期など）はまだ入れていない。
- 目標を `--objective violations` にしても、今の行動方針と改正幅では
  「違反は減るが配達が悪化する」というグッドハート型の暴走は再現しない。
- 委員会はルールベース（隣の案をすべて試す）。LLM による改正案はまだない。
- 運用モード（分散 ⇄ 集中）の切替、Unity・実機での再生はまだない。
- `town/contract_engine.py` は raspimouse-swarm-simulator の
  `cadl/runtime/engine.py`（commit 848541f）の複製。
