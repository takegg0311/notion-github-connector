# notion-github-connector

GitHub アカウントが所有するリポジトリと、その中の Issue・Pull Request を取得し、Notion の 3 つのデータベース（RepositoryDB / IssueDB / PullRequestDB）へ同期するバッチ CLI です。

## 必要なトークン

### GitHub (`GITHUB_TOKEN`)

fine-grained PAT を推奨します。

- Resource owner: 自分のアカウント
- Repository access: All repositories
- Permissions（Read-only）: Metadata, Issues, Pull requests

classic PAT を使う場合は `repo` スコープが必要です。

### Notion (`NOTION_TOKEN`)

[Notion インテグレーション](https://www.notion.so/my-integrations)を作成し、Internal Integration Token を `.env` に設定します。3 つのデータベースすべてで、インテグレーションを「接続」してください（各 DB ページ右上の `...` → Connections）。

## データベースの準備

既存 DB を使います。サンプルページは削除して、空の DB から始めてください（移行処理はありません）。

プロパティ名と型は次のとおり固定です（コード側で変更できません）。

### RepositoryDB

| プロパティ | 型 |
|---|---|
| Name | title |
| URL | url |
| Created | date |
| Updated | date（GitHub の `pushedAt`） |
| GitHub ID | rich_text |

### IssueDB

| プロパティ | 型 |
|---|---|
| Name | title |
| Number | number |
| Status | status |
| URL | url |
| Created | date |
| Updated | date |
| Repository | relation → RepositoryDB |
| GitHub ID | rich_text |

`Status` は Notion の **status 型**にしてください（select 型ではありません）。status 型は API から選択肢を追加できないため、同期前に Notion 上で次の名前のオプションを手動で用意してください。

- IssueDB: `Open` / `Closed` / `Closed as not planned` / `Closed as duplicate`
- PullRequestDB: `Open` / `Merged` / `Closed`

### PullRequestDB

IssueDB と同じプロパティに、次を追加します。

| プロパティ | 型 |
|---|---|
| Issues | relation → IssueDB（双方向推奨） |

`Issues` は dual property（双方向）にすることを推奨します。IssueDB 側の逆プロパティ名は **Pull Requests** にしてください。片方向 relation でも同期は動作します。

## 環境変数

`.env.example` をコピーして `.env` を作成します。

| 変数 | 説明 |
|---|---|
| `GITHUB_TOKEN` | GitHub PAT |
| `NOTION_TOKEN` | Notion インテグレーショントークン |
| `NOTION_REPOSITORY_DATABASE_ID` | RepositoryDB の ID |
| `NOTION_ISSUE_DATABASE_ID` | IssueDB の ID |
| `NOTION_PULL_REQUEST_DATABASE_ID` | PullRequestDB の ID |
| `TIMEZONE` | Ofelia のスケジュール用タイムゾーン（既定 `Asia/Tokyo`。同期アプリは未使用） |
| `OFELIA_SCHEDULE` | Ofelia の cron（空なら `@hourly`） |

## 手動実行

```bash
docker compose run --rm sync
```

## 定期実行

[Ofelia](https://github.com/mcuadros/ofelia) が常駐し、既定では毎時 `sync` コンテナを起動してバッチを実行します。

```bash
cp .env.example .env   # トークンと DB ID を設定
docker compose up -d
```

`up` の時点で compose が `sync` を1回起動するため、初回もバッチが走ります（`up --build` も同様）。実行が終わった `sync` は停止したまま（`exited`）で、次のスケジュールまで常駐しません。

停止・削除:

```bash
docker compose down
```

スケジュールは `.env` の `OFELIA_SCHEDULE` で変更します（未設定なら `@hourly`）。Ofelia 0.3.x では **秒から始める 6 フィールド**の cron 式を使います（毎時0分は `0 0 * * * *`）。`@every 1m` も指定できます。5 フィールドだけ書くとエラーにならず、左に 1 つずつずれて解釈されます（先頭が秒=0、末尾の曜日は `*`）。例: `0 * * * *` は毎分、`0 3 * * *` は毎時3分（日次3:00のつもりだと意図とずれます）。ラベルは Ofelia 起動時に一度だけ読み込まれるため、変更後は `docker compose up -d` で `ofelia` を再作成してください。タイムゾーンは `TIMEZONE`（既定 `Asia/Tokyo`）を Ofelia の `TZ` に渡します。同期アプリ自体は `TIMEZONE` を使いません。

ログ:

```bash
docker compose logs ofelia
docker compose logs sync
```

`ofelia` は `/var/run/docker.sock` をマウントして Docker API でコンテナを起動します。`:ro` でも API 経由では実質ホストの root 相当の操作が可能です。信頼できる環境でのみ利用してください。

バッチ実行中に `docker compose up -d --build`（`sync` の再作成）や `docker compose down` を行うと、処理は途中で止まります。同期は冪等なので、中断した分は次の実行で回復します。操作前に `docker compose ps` で `sync` が実行中でないことを確認してください。

手動実行（`docker compose run --rm sync`）は、定期実行とは別コンテナのため Ofelia の `no-overlap` 対象外です。定期実行と重なると、同じ `GitHub ID` のページが重複作成され、自動では消えません。解消方法は「同時実行について」を参照してください。手動実行の前に `docker compose ps` で実行中でないことを確かめてください。

## 同期の挙動（概要）

- トークン所有者が owner の全リポジトリ（private 含む、fork 除外、archived 含む）を毎回フル取得し、Notion 側は `GitHub ID` で突き合わせて差分だけ書き込みます。
- Issue と PR の関連は、GitHub の `closingIssuesReferences` と PR 本文の closing キーワード解析の和集合です（単なる `#N` 言及は含めません）。
- GitHub 側で削除された項目や、fork 化・移管で対象外になった項目の Notion ページは削除しません。
- ページ本文（ブロック）やラベル・担当者など、上記プロパティ以外は触りません。

## 同時実行について

同じ DB に対して 2 プロセス以上を同時に走らせると、同じ `GitHub ID` のページが重複作成される可能性があります。WARNING で duplicate が出た場合は、`created_time` が新しい方のページを手で削除してください。

## 開発・検証

```bash
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Docker:

```bash
cp .env.example .env   # トークンは空のままで可（初期化失敗の確認用）
docker compose run --rm sync
```
