import argparse
import json
import os
import time
from dataclasses import dataclass
from pprint import pprint
from typing import List

import boto3
import botocore
import requests
from awscli.customizations.cloudformation.yamlhelper import yaml_parse

ENV = os.getenv("ENV", "")
PROJECT_NAME = os.getenv("ProjectName", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
URL = os.getenv("URL", "")

SIGNED_URL_TIMEOUT_SECONDS = 60 * 5  # 5 minutes

YML_DIR = "./cfn"
PARAM_DIR = "./param"


@dataclass
class YmlItem:
    yml_filename: str
    region: str

    def __str__(self) -> str:
        return f"{self.yml_filename} @ {self.region}"


# Execute from the top item of YML_ORDER
YML_ORDER = [
    YmlItem("vpc.yml", "ap-northeast-1"),
    YmlItem("s3.yml", "ap-northeast-1"),
]


def main(is_dryrun=False):
    """main function

    Args:
        is_dryrun (bool, optional): dryrun or not. Defaults to False.
    """
    param_path = os.path.join(PARAM_DIR, f"{ENV}-parameters.json")
    with open(param_path, "r", encoding='UTF-8') as f:
        all_param_dic = json.load(f)["Parameters"]

    if is_dryrun:
        runstr = "dryrun"
        result = dryrun(all_param_dic)
        print("result:")
        pprint(result, indent=4)
    else:
        runstr = "deploy"
        result = deploy(all_param_dic)
        print("result:")
        pprint(result, indent=4)

    message_prefix = f"***** {ENV} {runstr} result *****"
    message = f"""{message_prefix}

    {json.dumps(result, indent=4)}
    """

    if URL != "":
        # remove past dryrun results on PR
        clean_before_ci_pull_request_comments(message_prefix)

        # post dryrun results to PR
        post_to_pull_request(message)


def dryrun(all_param_dic: dict) -> dict:
    """dryrun(just create changeset)

    Args:
        all_param_dic (dict): parameter dictionary

    Returns:
        dict: result dictionary(yml_filename to result)
    """
    result = {}
    for yml_item in YML_ORDER:
        print(f"***** {yml_item} start *****")
        result[str(yml_item)] = process_yml(
            yml_item, all_param_dic, deploys=False)
        print(f"***** {yml_item} finished *****")
    return result


def deploy(all_param_dic: dict) -> dict:
    """deploy(create changeset and execute it)

    Args:
        all_param_dic (dict): parameter dictionary

    Returns:
        dict: result dictionary(yml_filename to result)
    """
    result = {}
    for yml_item in YML_ORDER:
        print(f"***** {yml_item} start *****")
        result[str(yml_item)] = process_yml(
            yml_item, all_param_dic, deploys=True)
        print(f"***** {yml_item} finished *****")
    return result


def process_yml(yml_item: YmlItem, all_param_dic: dict, deploys: bool) -> List:
    """process a yml file

    Args:
        yml_item (YmlItem): yml information
        all_param_dic (dict): parameter dictionary
        deploys (bool): whether it is deploy or dryrun

    Returns:
        List: _description_
    """
    client = boto3.client('cloudformation', region_name=yml_item.region)
    yml_path = os.path.join(YML_DIR, yml_item.yml_filename)

    with open(yml_path, "r", encoding='UTF-8') as file:
        yml_str = file.read()
        parsed = yaml_parse(yml_str)
        use_params = []
        if "Parameters" in parsed:
            use_params = list(parsed["Parameters"])

    formatted_param = create_param(all_param_dic, use_params)
    print("formatted_param:")
    pprint(formatted_param, indent=4)
    stack_suffix, _ = os.path.splitext(yml_item.yml_filename)
    stack_name = f'{ENV}-{PROJECT_NAME}-{stack_suffix}'

    middle_name = "deploy" if deploys else "dryrun"
    change_set_name = f"{stack_suffix}-{middle_name}-{int(time.time())}"

    yml_url = upload_yml_to_s3(yml_path, yml_item.region)
    if not is_stack_exists(client, stack_name):
        if deploys:
            create_stack(client, stack_name, yml_url, formatted_param)
        else:
            return [f"{stack_name} Stack does not exist"]

    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/cloudformation.html#CloudFormation.Client.create_change_set
    create_response = client.create_change_set(
        StackName=stack_name,
        TemplateURL=yml_url,
        UsePreviousTemplate=False,
        Parameters=formatted_param,
        Capabilities=[
            'CAPABILITY_NAMED_IAM'
        ],
        ChangeSetName=change_set_name,
        ChangeSetType='UPDATE',
    )
    print(f"create_change_set response: {create_response.get('Id', '')}")

    waiter = client.get_waiter('change_set_create_complete')
    try:
        waiter.wait(
            ChangeSetName=change_set_name,
            StackName=stack_name,
            WaiterConfig={
                'Delay': 3,
                'MaxAttempts': 50
            }
        )

        desc_response = client.describe_change_set(
            ChangeSetName=change_set_name,
            StackName=stack_name,
        )
        print("describe_change_set response Changes:")
        pprint(desc_response["Changes"], indent=4)

        if not deploys:
            return [x["ResourceChange"] for x in desc_response["Changes"]]

        exec_response = client.execute_change_set(
            ChangeSetName=change_set_name,
            StackName=stack_name
        )
        print("execute_change_set response:")
        pprint(exec_response, indent=4)

        waiter = client.get_waiter('stack_update_complete')
        waiter.wait(
            StackName=stack_name,
            WaiterConfig={
                'Delay': 3,
                'MaxAttempts': 50
            }
        )

        return [x["ResourceChange"] for x in desc_response["Changes"]]

    except botocore.exceptions.WaiterError as err:
        print("fail-on-empty-changeset. err: ", err)
        return []
    except Exception as err:
        print(f"err: {err}")
        return [{"error": str(err)}]


def upload_yml_to_s3(yml_filename: str, region: str) -> str:
    """upload yml file to s3 and get presigned url

    Args:
        yml_filename (str): yml file name to upload
        region (str): target s3 region

    Returns:
        str: yml presigned url
    """
    s3_client = boto3.client('s3', region_name=region)
    s3_destination_bucket = f"{ENV}-{PROJECT_NAME}-infra-cfn"
    s3_client.upload_file(
        yml_filename, s3_destination_bucket, yml_filename)
    s3_source_signed_url = s3_client.generate_presigned_url('get_object',
                                                            Params={
                                                                'Bucket': s3_destination_bucket,
                                                                'Key': yml_filename
                                                            },
                                                            ExpiresIn=SIGNED_URL_TIMEOUT_SECONDS)
    return s3_source_signed_url


def create_param(param_master: dict, params: List[str]) -> List[dict]:
    """create parameter dictionary from parameter master and parameter list

    Args:
        param_master (dict): includes all parameters
        params (List[str]): parameters to use for a yml file

    Returns:
        List[dict]: list of parameter key-value dictionary to use for a yml file
    """
    result = []
    for param in params:
        if param in param_master:
            result.append({
                'ParameterKey': param,
                'ParameterValue': param_master[param],
            })
    return result


def is_stack_exists(client, stack_name: str) -> bool:
    """check whether a stack exists

    Args:
        client (client): boto3 cloudformation client
        stack_name (str): stack name
    Returns:
        bool: whether the stack exists
    """
    try:
        client.describe_stacks(StackName=stack_name)
        return True
    except botocore.exceptions.ClientError as err:
        if err.response['Error']['Code'] == 'ValidationError':
            return False
        raise


def create_stack(client, stack_name: str, template_url: str, parameters: List) -> None:
    """create a stack

    Args:
        client (clinet): boto3 cloudformation client
        stack_name (str): stack name
        template_url (str): CFn template url
        parameters (List): parameters to use for a yml file
    """
    client.create_stack(
        StackName=stack_name,
        TemplateURL=template_url,
        Capabilities=['CAPABILITY_NAMED_IAM'],
        Parameters=parameters
    )
    print(f"create_stack started: {stack_name}")

    waiter = client.get_waiter('stack_create_complete')
    waiter.wait(
        StackName=stack_name,
        WaiterConfig={
            'Delay': 3,
            'MaxAttempts': 50
        }
    )
    print(f"create_stack finished: {stack_name}")


def clean_before_ci_pull_request_comments(message_prefix: str) -> None:
    """delete all ci PR comments before the latest commits
    """
    comments = fetch_pull_request_comments()
    for comment in comments:
        if should_delete_comment(comment, message_prefix):
            delete_pull_request_comment(comment["url"])


def fetch_pull_request_comments() -> dict:
    """fetch pull request comments
    """
    response = requests.get(URL, headers={
        "Authorization": f"token {GITHUB_TOKEN}"})

    # print("fetch_pull_request_comments response:")
    # pprint(response.json(), indent=4)  # too long to print
    return response.json()


def delete_pull_request_comment(comment_url: str) -> None:
    """delete_pull_request_comment by id

    Args:
        id (str): pull request comment id
    """
    response = requests.delete(comment_url, headers={
        "Authorization": f"token {GITHUB_TOKEN}"})
    print("delete_pull_request_comment response:")
    print(response)


def should_delete_comment(comment: dict, message_prefix: str) -> bool:
    """check whether a comment should be deleted

    Args:
        comment (dict): pull request comment

    Returns:
        bool: whether the comment should be deleted
    """
    is_user_bot = comment["user"]["login"] == "github-actions[bot]"
    is_env_match = comment["body"].startswith(message_prefix)
    return is_user_bot and is_env_match


def post_to_pull_request(body: str) -> None:
    """post to pull_request_comments

    Args:
        body (str): comment body
    """
    response = requests.post(URL, json={"body": body}, headers={
        "Authorization": f"token {GITHUB_TOKEN}"})
    print("post_to_pull_request response:")
    pprint(response.json(), indent=4)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--dryrun', action='store_true')
    args = parser.parse_args()
    main(args.dryrun)
