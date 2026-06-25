from storage.store import ResearchStore

store = ResearchStore()
print(store.get_experiment_runs(5))
store.close()
