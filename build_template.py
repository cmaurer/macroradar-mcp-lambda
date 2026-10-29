"""Bundles src/tools.json + src/server.py into build/index.py and writes the CloudFormation template.

The Lambda code is embedded inline (Code.ZipFile), so the whole deploy is one template.
To refresh tools.json from a newer @macroradar/mcp release, see README.md.
"""
import json
import pathlib

root = pathlib.Path(__file__).parent
tools = json.loads((root / "src" / "tools.json").read_text())
tools_compact = json.dumps(tools, separators=(",", ":"), ensure_ascii=False)
assert "'''" not in tools_compact and not tools_compact.endswith("\\")

bundle = "TOOLS_JSON = r'''" + tools_compact + "'''\n\n" + (root / "src" / "server.py").read_text()
compile(bundle, "index.py", "exec")
(root / "build").mkdir(exist_ok=True)
(root / "build" / "index.py").write_text(bundle)

P = "Mr"  # logical-id prefix
template = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": "MacroRadar MCP server: public Lambda function URL with bearer-token auth",
    "Resources": {
        f"{P}Secret": {
            "Type": "AWS::SecretsManager::Secret",
            "Properties": {
                "Name": "macroradar-mcp/config",
                "Description": "bearer_token (generated) guarding the MacroRadar MCP endpoint",
                "GenerateSecretString": {"SecretStringTemplate": "{}", "GenerateStringKey": "bearer_token",
                                         "PasswordLength": 48, "ExcludePunctuation": True},
            },
        },
        f"{P}LogGroup": {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": "/aws/lambda/macroradar-mcp", "RetentionInDays": 30},
        },
        f"{P}Role": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [
                    {"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]},
                "Policies": [{"PolicyName": "macroradar-mcp-minimal", "PolicyDocument": {
                    "Version": "2012-10-17", "Statement": [
                        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                         "Resource": {"Fn::Sub": f"${{{P}LogGroup.Arn}}"}},
                        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue",
                         "Resource": {"Ref": f"{P}Secret"}}]}}],
            },
        },
        f"{P}Function": {
            "Type": "AWS::Lambda::Function",
            "DependsOn": [f"{P}LogGroup"],
            "Properties": {
                "FunctionName": "macroradar-mcp",
                "Description": "MacroRadar MCP server (stateless streamable HTTP), port of @macroradar/mcp",
                "Runtime": "python3.13",
                "Architectures": ["arm64"],
                "Handler": "index.handler",
                "MemorySize": 512,
                "Timeout": 90,
                "Role": {"Fn::GetAtt": [f"{P}Role", "Arn"]},
                "Environment": {"Variables": {"SECRET_ARN": {"Ref": f"{P}Secret"}}},
                "Code": {"ZipFile": bundle},
            },
        },
        f"{P}Url": {
            "Type": "AWS::Lambda::Url",
            "Properties": {"AuthType": "NONE", "TargetFunctionArn": {"Fn::GetAtt": [f"{P}Function", "Arn"]}},
        },
        f"{P}UrlInvokePermission": {
            "Type": "AWS::Lambda::Permission",
            "Properties": {"FunctionName": {"Ref": f"{P}Function"}, "Action": "lambda:InvokeFunctionUrl",
                           "Principal": "*", "FunctionUrlAuthType": "NONE"},
        },
        f"{P}InvokeViaUrlPermission": {
            "Type": "AWS::Lambda::Permission",
            "Properties": {"FunctionName": {"Ref": f"{P}Function"}, "Action": "lambda:InvokeFunction",
                           "Principal": "*", "InvokedViaFunctionUrl": True},
        },
    },
    "Outputs": {
        "FunctionUrl": {"Value": {"Fn::GetAtt": [f"{P}Url", "FunctionUrl"]}},
        "SecretArn": {"Value": {"Ref": f"{P}Secret"}},
    },
}

(root / "build" / "template.json").write_text(json.dumps(template, indent=2) + "\n")
compact = json.dumps(template, separators=(",", ":"), ensure_ascii=True).replace("'", "\\u0027")
(root / "build" / "template.min.json").write_text(compact)
print(f"bundle {len(bundle)} chars; compact template {len(compact)} bytes (CLI inline limit 51200)")
