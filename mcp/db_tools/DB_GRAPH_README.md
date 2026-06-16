# Database Graph Builder

Builds a fresh directed graph from the current Microsoft SQL Server catalog on every run.

## Install

```bash
python -m pip install -r requirements-db-graph.txt
```

You also need Microsoft ODBC Driver 18 for SQL Server installed on the machine.

## Run with an environment variable

### Linux/macOS

```bash
export DB_CONNECTION_STRING='DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=my_db;UID=my_user;PWD=my_password;Encrypt=yes;TrustServerCertificate=yes'
python build_db_graph.py
```

### PowerShell

```powershell
$env:DB_CONNECTION_STRING = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=my_db;UID=my_user;PWD=my_password;Encrypt=yes;TrustServerCertificate=yes"
python .\build_db_graph.py
```

## Or pass the connection string directly

```bash
python build_db_graph.py \
  --connection-string 'DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost;DATABASE=my_db;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes'
```

## Output

By default, files are rebuilt in:

```text
.agent-os/db/
├── db_graph.graphml
├── db_graph.json
└── db_graph.md
```

Use a different directory with:

```bash
python build_db_graph.py --output-dir ./database-graph
```

To include complete SQL definitions for functions and procedures:

```bash
python build_db_graph.py --include-definitions
```

Definitions are excluded by default. A SHA-256 hash is still stored so changes can be detected without storing the full SQL text.

## Graph model

Nodes:

- Table
- Column
- Function
- Procedure

Directed relationships:

- Stores: Table -> Column
- Links: Foreign-key Column -> Referenced key Column
- Creates: Procedure/Function -> referenced Table or Column

`Creates` is populated from `sys.sql_expression_dependencies`. In practical terms, it represents a SQL dependency. Dynamic SQL and some non-schema-bound column references may not be discoverable from that catalog view.
