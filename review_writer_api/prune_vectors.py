"""Prune unreferenced SQLite vector snapshots; never removes active versions."""
import argparse
import json
import uuid
from review_writer_api.config import ApiSettings
from review_writer_api.database import User, create_session_factory, database_session
from review_writer_api.vector_store import LocalVectorStore
from review_writer_api.workspaces import HostedWorkspaceManager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=uuid.UUID)
    args = parser.parse_args()
    settings = ApiSettings.from_env()
    sessions, engine = create_session_factory(settings.database_url)
    try:
        with database_session(sessions) as session:
            user = session.get(User, args.user_id)
            if user is None or user.status != "active":
                parser.error("Active user UUID not found.")
        store = LocalVectorStore(sessions, HostedWorkspaceManager(settings.hosted_workspace_root or
            settings.review_root/".review-writer"/"hosted-workspaces"), settings.sqlite_vector_extension)
        result = {"removed_versions": store.prune(args.user_id)}
        print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
