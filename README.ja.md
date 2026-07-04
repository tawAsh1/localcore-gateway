# localcore-gateway

[![PyPI](https://img.shields.io/pypi/v/localcore-gateway.svg)](https://pypi.org/project/localcore-gateway/)
[![CI](https://github.com/tawAsh1/localcore-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/tawAsh1/localcore-gateway/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python ≥3.11](https://img.shields.io/badge/python-%E2%89%A53.11-blue.svg)](pyproject.toml)

[English](README.md) | 日本語

**AWS Bedrock AgentCore Gateway** の「十分に忠実な」ローカル再実装。差し替え可能な**ローカル Lambda バックエンド**付き。
エージェント ↔ ゲートウェイ ↔ Lambda の統合をすべて手元のマシンで開発してテストし、そのまま同じ MCP クライアントを、コード変更なしで本物の AWS ゲートウェイに向けられます。

AgentCore Gateway には公式のローカルエミュレータがありません(AWS の `agentcore dev` は *Runtime* 用で、Gateway 用ではありません)。
このプロジェクトはその欠落を埋めます。

> **0.x は不安定です。** CLI フラグと設定スキーマは 1.0 までマイナーリリース間で変わる可能性があります。
> 依存する場合はバージョンを固定してください。

## 再現しているもの

- **`/mcp` での MCP Streamable-HTTP**:本物のゲートウェイと同じワイヤサーフェスです([FastMCP](https://github.com/jlowin/fastmcp) 3.x サーバー上に構築。JSON-RPC の手書き実装はしていません)。
- **ターゲット集約**:すべての `(target, tool)` が `target___tool` という名前の 1 つの MCP ツールとして公開されます(AgentCore のトリプルアンダースコア規約)。
- **AgentCore Lambda コントラクト**:ツール引数は Lambda の event として渡され、ツールの識別子は `context.client_context.custom['bedrockAgentCoreToolName']` で届き、Lambda の戻り値がツール結果になります。
- **OpenAPI ターゲット**:REST API の仕様が MCP ツールになります。
  ツール名はオペレーションの `operationId` そのままで、本物のゲートウェイと同じくスラッグ化しません。
  仕様側の security 定義は無視されます(認証は別途設定)。
- **MCP パススルーターゲット**:別の MCP サーバーのツールを、リモートのツール名を無加工で使ってプロキシします。
  Streamable HTTP が AgentCore に忠実なモードで、ローカル専用の便宜機能として stdio の `command` モードもあります(AWS 側に対応物なし)。
- **ターゲット再同期**:`lcgw sync` は `SynchronizeGatewayTargets` の対応物で、稼働中のゲートウェイ上で MCP ターゲットが上流のツールを再発見します。
  再起動は不要です(AWS の非同期 202 スタイル API と違い、同期実行)。
  さらに AWS 側に対応物のない開発ループ用のおまけとして、`lcgw tail` が全ツール呼び出しをライブ表示します。

```yaml
targets:
  - type: mcp
    name: mytools
    url: http://127.0.0.1:9000/mcp
    auth: { type: bearer, value: "${TOKEN}" }   # 環境変数から展開されます
```

## 本物の AWS を混ぜるハイブリッドデバッグ

`aws` extra をインストールすると、ローカルゲートウェイに本物の AWS リソースをローカルターゲットと並べて混在させられます。
1 つのツールをローカルで反復開発しつつ、残りの本番ツールセットは本物のまま使えます。

- **`type: aws-gateway`**:デプロイ済みの AgentCore Gateway をプロキシします。
  そのツールは `remoteTarget___tool` の名前そのまま(プレフィックスなし)で通ります。
  認証は bearer(OAuth/JWT)または SigV4(IAM)。
  AWS 側に対応物はなく、純粋にハイブリッドワークフロー用の機能です。
- **`lambda.backend: aws`**:第 3 の Lambda バックエンドです。
  ツールスキーマはローカル、ハンドラーはデプロイ済みの本物の関数で、AgentCore ClientContext コントラクトも CloudWatch ログ末尾の取得もそのまま働きます。
  副作用のある呼び出しが黙って二重実行されないよう、リトライは無効化しています。

[`examples/hybrid_config.yaml`](examples/hybrid_config.yaml) と[設定リファレンス](docs/configuration.md)を参照してください。

## テストとコントラクト(ローカル専用の追加機能)

- **モックターゲット**(`type: mock`):ツールを設定ファイルだけで宣言し、あらかじめ決めた応答やエラーを返します。
  実物のツールができる前にエージェント側を開発できます。
  AWS 側に対応物はありません。
- **コントラクトチェック**(`server.contract_checks: warn|error`):ツールの引数と結果を、宣言された JSON Schema に対してゲートウェイで検証します。
  スキーマとハンドラーのずれをデプロイ前に手元で検出できます。
  デフォルトはオフです(本物のゲートウェイは検証しないため)。
- **`localcore_gateway.testing`**:公開の pytest ヘルパーです。
  `serve_gateway` がゲートウェイ全体を一時ポートで起動し、`call_tool` で単発のアサーションが書けます。
  [Testing your handlers](docs/testing.md) を参照してください。

## ローカル Lambda バックエンド

| backend  | Docker | 忠実度 | 用途 |
|----------|--------|--------|------|
| `native` | 不要   | ターゲットごとに 1 サブプロセス(実プロセス分離のためモノレポでも安全)、忠実な `event`/`context`、エラーエンベロープ、CloudWatch 風ログ、ホットリロード、ハードタイムアウト | 高速な開発ループ |
| `sam`    | 必要   | `sam local start-lambda` 経由の本物の AWS Lambda Linux ランタイム | AWS 投入前の Linux ランタイム忠実性チェック |
| `aws`    | 不要   | デプロイ済みの本物の関数(`aws` extra と認証情報が必要) | 本番相当リソースに対するハイブリッドデバッグ |

## ドキュメント

- [Architecture](docs/architecture.md):リクエストフロー、AgentCore コントラクトのマッピング、コンポーネントマップ
- [Configuration reference](docs/configuration.md):全設定フィールド
- [Writing Lambda handlers](docs/lambda-handlers.md):ハンドラーコントラクト、マルチツール、エラー、ログ、native と sam の違い
- [Testing your handlers](docs/testing.md):`localcore_gateway.testing`、モックターゲット、pytest でのコントラクトチェック
- [CLI reference](docs/cli.md):`serve` / `dev` / `tools` / `invoke`
- [Connecting agents](docs/connecting-agents.md):MCP クライアントの接続、本物の AWS への昇格

## インストール

```bash
uv tool install localcore-gateway      # または: pipx install localcore-gateway
uvx --from localcore-gateway lcgw --help   # 使い捨て実行(インストール不要)
```

本物の AWS へのパススルー機能(`type: aws-gateway`、`lambda.backend: aws`)を使う場合は `aws` extra をインストールしてください。

```bash
uv tool install 'localcore-gateway[aws]'   # または: pip install 'localcore-gateway[aws]'
```

## クイックスタート

ハンドラーと設定ファイルだけで動きます。

```python
# handlers.py
def handler(event, context):
    return {"sum": event["a"] + event["b"]}
```

```yaml
# gateway.yaml
targets:
  - type: lambda
    name: demo
    lambda: { backend: native, handler: handlers.handler }
    tools:
      - name: add
        inputSchema:
          type: object
          properties: { a: { type: number }, b: { type: number } }
          required: [a, b]
```

```bash
lcgw tools  -c gateway.yaml
lcgw invoke -c gateway.yaml demo___add --data '{"a":2,"b":40}'
lcgw serve  -c gateway.yaml            # MCP は http://127.0.0.1:8080/mcp
lcgw dev    -c gateway.yaml            # 同上 + ホットリロード
lcgw tail   -c gateway.yaml            # 呼び出しのライブ表示(稼働中のゲートウェイ)
lcgw sync   -c gateway.yaml            # MCP ターゲットの再同期(稼働中のゲートウェイ)
```

任意の MCP クライアントを `http://127.0.0.1:8080/mcp` に向けてください。
より充実した例(マルチターゲット、`math_handlers.py`、Strands エージェント、`mcp_config.yaml` による MCP パススルー)は [`examples/`](examples/) にあります。

### ソースから動かす(開発用)

```bash
git clone https://github.com/tawAsh1/localcore-gateway && cd localcore-gateway
uv sync
uv run pytest
uv run lcgw serve -c examples/config.yaml
```

### `sam` バックエンドを使う

SAM プロジェクトで `sam local start-lambda` を起動し、ターゲットに次を設定します。

```yaml
lambda:
  backend: sam
  sam_endpoint: http://127.0.0.1:3001
  sam_function: DemoFunction
```

## 設定

[`examples/config.yaml`](examples/config.yaml) を参照してください。
ターゲットは Lambda(`backend`、`handler`/`sam_function`、`memory_mb`、`timeout_sec`、`env`)と、それが担うツール群(それぞれ明示的な JSON Schema 付き)を宣言します。
1 つの Lambda で複数のツールを担え、ハンドラーは `bedrockAgentCoreToolName` で分岐します。

## 既知の制限

- `native` はハンドラーをサブプロセスで実行しますが、**セキュリティサンドボックスではありません**(ファイルシステムとネットワークの隔離なし)。
  信頼できるコードにのみ使ってください。
- `native` はターゲットごとに呼び出しを直列化します(ウォームな実行環境が 1 つ)。
  Lambda の同時実行環境スケーリングは模していません。
- `sam` の呼び出しごとのログは `sam local` のコンソールに出ます(Invoke API の外側です)。
- AgentCore 組み込みのセマンティックツール検索(`x_amz_bedrock_agentcore_search`)は未実装です(意図的な省略)。
- ターゲット種別は Lambda、OpenAPI、MCP パススルー、AWS ゲートウェイパススルーを実装済みで(加えてローカル専用のモックターゲット)、Smithy は未対応です。
  アウトバウンド認証(OpenAPI と MCP パススルー共通)は静的 API キー(ヘッダーまたはクエリ)とベアラートークンをカバーします。
  OAuth 2LO はスコープ外です。
- ハイブリッド機能(`type: aws-gateway`、`lambda.backend: aws`)は `aws` extra と本物の AWS 認証情報が必要で、AWS 側の挙動(コールドスタート、IAM、クォータ)に従います。
  ローカルでは何もエミュレートしません。

## ライセンス

[Apache License 2.0](LICENSE)。
帰属表示と下記の商標に関する免責は [`NOTICE`](NOTICE) を参照してください。

## 商標と免責

これは非公式のコミュニティプロジェクトです。
**Amazon Web Services, Inc. およびその関連会社による提携、承認、後援は受けていません。**

「AWS」「Amazon Web Services」「Amazon Bedrock」「Amazon Bedrock AgentCore」「AWS Lambda」は Amazon.com, Inc. またはその関連会社の商標です。
本プロジェクトでは、相互運用しローカルに再実装する対象の AWS サービスを正確に記述するための指名的使用に限って用いています。
AWS の商標、ロゴ、トレードドレスを本プロジェクトの名称やブランディングとして使用していません。
