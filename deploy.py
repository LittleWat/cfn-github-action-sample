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
    with open(f"./param/{ENV}-parameters.json") as f:
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

    message = f"""
    ***** {ENV} {runstr} result *****

    {json.dumps(result, indent=4)}
    """

    if URL != "":
        post_to_pull_request(message)


def dryrun(all_param_dic: dict):
    result = {}
    for yml_item in YML_ORDER:
        print(f"***** {yml_item} start *****")
        result[str(yml_item)] = process_yml(
            yml_item, all_param_dic, deploys=False)
        print(f"***** {yml_item} finished *****")
    return result


def deploy(all_param_dic: dict):
    result = {}
    for yml_item in YML_ORDER:
        print(f"***** {yml_item} start *****")
        result[str(yml_item)] = process_yml(
            yml_item, all_param_dic, deploys=True)
        print(f"***** {yml_item} finished *****")
    return result


def process_yml(yml_item: YmlItem, all_param_dic: dict, deploys: bool) -> List:
    client = boto3.client('cloudformation', region_name=yml_item.region)

    with open(yml_item.yml_filename) as file:
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

    yml_url = upload_yml_to_s3(yml_item.yml_filename, yml_item.region)
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


def upload_yml_to_s3(yml_filename: str, region: str):
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


def create_param(param_master, params):
    result = []
    for param in params:
        if param in param_master:
            result.append({
                'ParameterKey': param,
                'ParameterValue': param_master[param],
            })
    return result


def is_stack_exists(client, stack_name):
    try:
        client.describe_stacks(StackName=stack_name)
        return True
    except botocore.exceptions.ClientError as err:
        if err.response['Error']['Code'] == 'ValidationError':
            return False
        raise


def create_stack(client, stack_name, template_url, parameters):
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


def post_to_pull_request(body):
    response = requests.post(URL, json={"body": body}, headers={
        "Authorization": f"token {GITHUB_TOKEN}"})
    print("response:")
    pprint(response.json(), indent=4)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--dryrun', action='store_true')
    args = parser.parse_args()
    main(args.dryrun)
