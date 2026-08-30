# SQLAlchemy 1.4 → 2.0 — Changelog excerpts (bundled by BreakBot)

Source: SQLAlchemy 2.0 Major Migration Guide — https://docs.sqlalchemy.org/en/20/changelog/migration_20.html

## connectionless-execution
### “Implicit” and “Connectionless” execution, “bound metadata” removed
- The `Engine.execute()` method is removed in 2.0. All statement execution is performed by `Connection.execute()` (Core) or `Session.execute()` (ORM).
- `MetaData(bind=engine)` (bound metadata) is removed; the `bind` keyword is no longer accepted.
- Passing a raw string to `Connection.execute()` or `Session.execute()` is removed; wrap SQL text in `text()`, or use `Connection.exec_driver_sql()`.
- Library-level (but not driver-level) autocommit is removed; wrap writes in `with engine.begin() as conn:` (or call `conn.commit()` explicitly).
- The 1.x idiom is replaced by:
  ```python
  with engine.begin() as conn:
      conn.execute(text("INSERT INTO foo (id) VALUES (1)"))
  with engine.connect() as conn:
      result = conn.execute(select(foo.c.id))
  ```

## select-constructor
### `select()` no longer accepts varied constructor arguments
- The legacy calling style `select([col1, col2])` (a list of columns) is removed; columns are passed positionally: `select(col1, col2)`.
- `insert()`/`update()`/`delete()` DML no longer accept keyword constructor arguments such as `values(...)` at construction; chain `.values(...)`.

## orm-query-unified
### ORM Query unified with Core select
- Legacy `Session.query(Model)` usage remains only as a legacy compatibility shim; 2.0 style is `session.execute(select(Model)).scalars()`.
- Rows returned by `Session.execute()` are `Row` objects and are not uniquified by default; call `.scalars()` (or `.unique().scalars()`) when fetching ORM entities.
- Joining/loading on relationships uses class attributes, not strings (`joinedload(User.address)` not `joinedload("address")`).

## query-get-moved
### ORM Query — `get()` method moves to Session
- `Query.get()` (i.e. `session.query(Model).get(id)`) is legacy in 2.0 and emits `LegacyAPIWarning`; the primary-key lookup is now `Session.get(Model, id)`.

## declarative-first-class
### Declarative becomes a first-class API
- `sqlalchemy.ext.declarative.declarative_base()` is legacy; import from `sqlalchemy.orm` (`from sqlalchemy.orm import declarative_base`), or use the new `DeclarativeBase` base class:
  ```python
  from sqlalchemy.orm import DeclarativeBase
  class Base(DeclarativeBase): ...
  ```

## session-autocommit
### Autocommit mode removed from Session
- `sessionmaker(autocommit=True)` / `Session(autocommit=True)` are removed; the Session always uses autobegin and requires explicit `commit()`.
- Session “subtransaction” behavior (`begin_nested` aside) is removed.
