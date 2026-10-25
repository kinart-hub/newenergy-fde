# -*- coding: utf-8 -*-
"""首帖发布辅助：通过 GraphQL 创建 GitHub Discussion。
令牌经 git credential fill 获取，仅在内存中使用，不打印、不落盘。"""
import json
import subprocess
import sys
import urllib.request

REPO_OWNER = "kinart-hub"
REPO_NAME = "newenergy-fde"
API = "https://api.github.com/graphql"


def get_token():
    q = "protocol=https\nhost=github.com\n\n"
    out = subprocess.run(["git", "credential", "fill"], input=q.encode(),
                         capture_output=True).stdout.decode()
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    return None


def gql(token, query, variables):
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "fde-ops",
        })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "categories"
    token = get_token()
    if not token:
        print("[错误] 无法从 git credential 获取令牌")
        return 1

    if mode == "categories":
        q = """query($o:String!,$n:String!){repository(owner:$o,name:$n){
          id discussionCategories(first:10){nodes{id name slug}}}}"""
        d = gql(token, q, {"o": REPO_OWNER, "n": REPO_NAME})
        if d.get("errors"):
            print(json.dumps(d["errors"], ensure_ascii=False))
            return 1
        repo = d["data"]["repository"]
        print("repo_id:", repo["id"])
        for c in repo["discussionCategories"]["nodes"]:
            print(f"  {c['slug']}\t{c['name']}\t{c['id']}")
        return 0

    if mode == "create":
        # python post_discussion.py create <category_id> <title_file> <body_file>
        cat_id, title, body = sys.argv[2], sys.argv[3], sys.argv[4]
        m = gql(token, """query($o:String!,$n:String!){repository(owner:$o,name:$n){id}}""",
                {"o": REPO_OWNER, "n": REPO_NAME})
        repo_id = m["data"]["repository"]["id"]
        t = open(title, encoding="utf-8-sig").read().strip()
        b = open(body, encoding="utf-8-sig").read()
        q = """mutation($r:ID!,$c:ID!,$t:String!,$b:String!){
          createDiscussion(input:{repositoryId:$r,categoryId:$c,title:$t,body:$b}){
            discussion{ url number } }}"""
        d = gql(token, q, {"r": repo_id, "c": cat_id, "t": t, "b": b})
        if d.get("errors"):
            print(json.dumps(d["errors"], ensure_ascii=False))
            return 1
        disc = d["data"]["createDiscussion"]["discussion"]
        print("created:", disc["url"], "#", disc["number"])
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
