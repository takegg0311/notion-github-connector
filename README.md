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

## 手動実行

```bash
docker compose run --rm sync
```

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
