from storage.sqlite_store import SQLiteStore

store = SQLiteStore("quant_research.db")
print(store.get_experiment_runs(5))
store.close()