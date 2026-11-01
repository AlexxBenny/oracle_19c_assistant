import argparse
from langchain_chroma import Chroma
from store import COLLECTION, CHROMA_PATH, get_embeddings, load_issues, issue_to_document

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    docs = [issue_to_document(x) for x in load_issues()]
    if not docs:
        raise SystemExit("No issue records found.")
    store = Chroma(collection_name=COLLECTION, embedding_function=get_embeddings(),
                   persist_directory=str(CHROMA_PATH))
    if args.rebuild:
        ids = store.get().get("ids", [])
        if ids:
            store.delete(ids=ids)
    existing = set(store.get().get("ids", []))
    new_docs = [d for d in docs if d.metadata["issue_id"] not in existing]
    if new_docs:
        store.add_documents(new_docs, ids=[d.metadata["issue_id"] for d in new_docs])
    print(f"JSON records: {len(docs)}; Chroma records: {store._collection.count()}")
    print(f"Persisted at: {CHROMA_PATH}")

if __name__ == "__main__":
    main()
