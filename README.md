# cfn-github-action-sample

Cloudformation with Github Actions sample repository.

You can

- Run CI(linter, dryrun) and see the result on pull request
  - example: https://github.com/LittleWat/cfn-github-action-sample/pull/2
- Deploy easily;
  - If you force-push to `dev-release` or `stg-release` branch will deploy to `dev` or `stg` environment like the command below;
    - `git push origin HEAD:dev-release -f`
  - `prd-release` is dangerous, so you need to make a pull request to deploy to `prd` environment.

## Run Locally

1. Download direnv

2. Copy `.env.template` to `.env` and edit `.env` based on your setting

3. Then run dryrun,

```
python deploy.py --dryrun
```

if dryrun result is ok, then deploy

```
python deploy.py
```