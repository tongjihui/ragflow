#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import logging
import json
import os
from flask import request
from flask_login import login_required, current_user
from api.db.services.llm_service import LLMFactoriesService, TenantLLMService, LLMService
from api import settings
from api.utils.api_utils import server_error_response, get_data_error_result, validate_request
from api.db import StatusEnum, LLMType
from api.db.db_models import TenantLLM
from api.utils.api_utils import get_json_result
from api.utils.file_utils import get_project_base_directory
from rag.llm import EmbeddingModel, ChatModel, RerankModel, CvModel, TTSModel


@manager.route('/factories', methods=['GET'])  # noqa: F821
@login_required
def factories():
    """
    列出所有可用的大预言模型(LLM)工厂及其支持的模型类型，过滤掉特定的工厂(如Youdao、FastEmbed和BAAI)
    并返回每个工厂支持的模型类型列表
    """
    try:
        fac = LLMFactoriesService.get_all()
        fac = [f.to_dict() for f in fac if f.name not in ["Youdao", "FastEmbed", "BAAI"]]
        llms = LLMService.get_all()
        mdl_types = {}
        for m in llms:
            if m.status != StatusEnum.VALID.value:
                continue
            if m.fid not in mdl_types:
                mdl_types[m.fid] = set([])
            mdl_types[m.fid].add(m.model_type)
        for f in fac:
            f["model_types"] = list(mdl_types.get(f["name"], [LLMType.CHAT, LLMType.EMBEDDING, LLMType.RERANK,
                                                              LLMType.IMAGE2TEXT, LLMType.SPEECH2TEXT, LLMType.TTS]))
        return get_json_result(data=fac)
    except Exception as e:
        return server_error_response(e)


@manager.route('/set_api_key', methods=['POST'])  # noqa: F821
@login_required
@validate_request("llm_factory", "api_key")
def set_api_key():
    """
    设置指定LLM工厂的API密钥，并验证密钥的有效性（针对嵌入式模型、聊天模型和重排序模型）
    如果验证通过，则保存或更新密钥配置
    """
    req = request.json
    # test if api key works
    chat_passed, embd_passed, rerank_passed = False, False, False
    factory = req["llm_factory"]
    msg = ""
    for llm in LLMService.query(fid=factory):
        if not embd_passed and llm.model_type == LLMType.EMBEDDING.value:
            assert factory in EmbeddingModel, f"Embedding model from {factory} is not supported yet."
            mdl = EmbeddingModel[factory](
                req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                arr, tc = mdl.encode(["Test if the api key is available"])
                if len(arr[0]) == 0:
                    raise Exception("Fail")
                embd_passed = True
            except Exception as e:
                msg += f"\nFail to access embedding model({llm.llm_name}) using this api key." + str(e)
        elif not chat_passed and llm.model_type == LLMType.CHAT.value:
            assert factory in ChatModel, f"Chat model from {factory} is not supported yet."
            mdl = ChatModel[factory](
                req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                m, tc = mdl.chat(None, [{"role": "user", "content": "Hello! How are you doing!"}],
                                 {"temperature": 0.9, 'max_tokens': 50})
                if m.find("**ERROR**") >= 0:
                    raise Exception(m)
                chat_passed = True
            except Exception as e:
                msg += f"\nFail to access model({llm.llm_name}) using this api key." + str(
                    e)
        elif not rerank_passed and llm.model_type == LLMType.RERANK:
            assert factory in RerankModel, f"Re-rank model from {factory} is not supported yet."
            mdl = RerankModel[factory](
                req["api_key"], llm.llm_name, base_url=req.get("base_url"))
            try:
                arr, tc = mdl.similarity("What's the weather?", ["Is it sunny today?"])
                if len(arr) == 0 or tc == 0:
                    raise Exception("Fail")
                rerank_passed = True
                logging.debug(f'passed model rerank {llm.llm_name}')
            except Exception as e:
                msg += f"\nFail to access model({llm.llm_name}) using this api key." + str(
                    e)
        if any([embd_passed, chat_passed, rerank_passed]):
            msg = ''
            break

    if msg:
        return get_data_error_result(message=msg)

    llm_config = {
        "api_key": req["api_key"],
        "api_base": req.get("base_url", "")
    }
    for n in ["model_type", "llm_name"]:
        if n in req:
            llm_config[n] = req[n]

    for llm in LLMService.query(fid=factory):
        llm_config["max_tokens"]=llm.max_tokens
        if not TenantLLMService.filter_update(
                [TenantLLM.tenant_id == current_user.id,
                 TenantLLM.llm_factory == factory,
                 TenantLLM.llm_name == llm.llm_name],
                llm_config):
            TenantLLMService.save(
                tenant_id=current_user.id,
                llm_factory=factory,
                llm_name=llm.llm_name,
                model_type=llm.model_type,
                api_key=llm_config["api_key"],
                api_base=llm_config["api_base"],
                max_tokens=llm_config["max_tokens"]
            )

    return get_json_result(data=True)


@manager.route('/add_llm', methods=['POST'])  # noqa: F821
@login_required
@validate_request("llm_factory")
def add_llm():
    """
    添加一个新的LLM配置，支持多种工厂（如VolcEngine、Tencent Cloud、HuggingFace等），
    并根据模型类型（如嵌入式、聊天、图像转文本等）进行测试，如果测试通过，则保存配置
    """
    req = request.json
    factory = req["llm_factory"]
    api_key = req.get("api_key", "x")
    llm_name = req["llm_name"]

    def apikey_json(keys):
        nonlocal req
        return json.dumps({k: req.get(k, "") for k in keys})

    if factory == "VolcEngine":
        # For VolcEngine, due to its special authentication method
        # Assemble ark_api_key endpoint_id into api_key
        api_key = apikey_json(["ark_api_key", "endpoint_id"])

    elif factory == "Tencent Hunyuan":
        req["api_key"] = apikey_json(["hunyuan_sid", "hunyuan_sk"])
        return set_api_key()

    elif factory == "Tencent Cloud":
        req["api_key"] = apikey_json(["tencent_cloud_sid", "tencent_cloud_sk"])
        return set_api_key()

    elif factory == "Bedrock":
        # For Bedrock, due to its special authentication method
        # Assemble bedrock_ak, bedrock_sk, bedrock_region
        api_key = apikey_json(["bedrock_ak", "bedrock_sk", "bedrock_region"])

    elif factory == "LocalAI":
        llm_name += "___LocalAI"

    elif factory == "HuggingFace":
        llm_name += "___HuggingFace"

    elif factory == "OpenAI-API-Compatible":
        llm_name += "___OpenAI-API"

    elif factory == "VLLM":
        llm_name += "___VLLM"

    elif factory == "XunFei Spark":
        if req["model_type"] == "chat":
            api_key = req.get("spark_api_password", "")
        elif req["model_type"] == "tts":
            api_key = apikey_json(["spark_app_id", "spark_api_secret", "spark_api_key"])

    elif factory == "BaiduYiyan":
        api_key = apikey_json(["yiyan_ak", "yiyan_sk"])

    elif factory == "Fish Audio":
        api_key = apikey_json(["fish_audio_ak", "fish_audio_refid"])

    elif factory == "Google Cloud":
        api_key = apikey_json(["google_project_id", "google_region", "google_service_account_key"])

    elif factory == "Azure-OpenAI":
        api_key = apikey_json(["api_key", "api_version"])

    llm = {
        "tenant_id": current_user.id,
        "llm_factory": factory,
        "model_type": req["model_type"],
        "llm_name": llm_name,
        "api_base": req.get("api_base", ""),
        "api_key": api_key,
        "max_tokens": req.get("max_tokens")
    }

    msg = ""
    mdl_nm = llm["llm_name"].split("___")[0]
    if llm["model_type"] == LLMType.EMBEDDING.value:
        assert factory in EmbeddingModel, f"Embedding model from {factory} is not supported yet."
        mdl = EmbeddingModel[factory](
            key=llm['api_key'],
            model_name=mdl_nm,
            base_url=llm["api_base"])
        try:
            arr, tc = mdl.encode(["Test if the api key is available"])
            if len(arr[0]) == 0:
                raise Exception("Fail")
        except Exception as e:
            msg += f"\nFail to access embedding model({mdl_nm})." + str(e)
    elif llm["model_type"] == LLMType.CHAT.value:
        assert factory in ChatModel, f"Chat model from {factory} is not supported yet."
        mdl = ChatModel[factory](
            key=llm['api_key'],
            model_name=mdl_nm,
            base_url=llm["api_base"]
        )
        try:
            m, tc = mdl.chat(None, [{"role": "user", "content": "Hello! How are you doing!"}], {
                "temperature": 0.9})
            if not tc and m.find("**ERROR**:") >= 0:
                raise Exception(m)
        except Exception as e:
            msg += f"\nFail to access model({mdl_nm})." + str(
                e)
    elif llm["model_type"] == LLMType.RERANK:
        assert factory in RerankModel, f"RE-rank model from {factory} is not supported yet."
        try:
            mdl = RerankModel[factory](
                key=llm["api_key"],
                model_name=mdl_nm,
                base_url=llm["api_base"]
            )
            arr, tc = mdl.similarity("Hello~ Ragflower!", ["Hi, there!", "Ohh, my friend!"])
            if len(arr) == 0:
                raise Exception("Not known.")
        except KeyError:
            msg += f"{factory} dose not support this model({mdl_nm})"
        except Exception as e:
            msg += f"\nFail to access model({mdl_nm})." + str(
                e)
    elif llm["model_type"] == LLMType.IMAGE2TEXT.value:
        assert factory in CvModel, f"Image to text model from {factory} is not supported yet."
        mdl = CvModel[factory](
            key=llm["api_key"],
            model_name=mdl_nm,
            base_url=llm["api_base"]
        )
        try:
            with open(os.path.join(get_project_base_directory(), "web/src/assets/yay.jpg"), "rb") as f:
                m, tc = mdl.describe(f.read())
                if not m and not tc:
                    raise Exception(m)
        except Exception as e:
            msg += f"\nFail to access model({mdl_nm})." + str(e)
    elif llm["model_type"] == LLMType.TTS:
        assert factory in TTSModel, f"TTS model from {factory} is not supported yet."
        mdl = TTSModel[factory](
            key=llm["api_key"], model_name=mdl_nm, base_url=llm["api_base"]
        )
        try:
            for resp in mdl.tts("Hello~ Ragflower!"):
                pass
        except RuntimeError as e:
            msg += f"\nFail to access model({mdl_nm})." + str(e)
    else:
        # TODO: check other type of models
        pass

    if msg:
        return get_data_error_result(message=msg)

    if not TenantLLMService.filter_update(
            [TenantLLM.tenant_id == current_user.id, TenantLLM.llm_factory == factory,
             TenantLLM.llm_name == llm["llm_name"]], llm):
        TenantLLMService.save(**llm)

    return get_json_result(data=True)


@manager.route('/delete_llm', methods=['POST'])  # noqa: F821
@login_required
@validate_request("llm_factory", "llm_name")
def delete_llm():
    """
    删除指定的LLM配置（通过工厂名称和模型名称定位）
    """
    req = request.json
    TenantLLMService.filter_delete(
        [TenantLLM.tenant_id == current_user.id, TenantLLM.llm_factory == req["llm_factory"],
         TenantLLM.llm_name == req["llm_name"]])
    return get_json_result(data=True)


@manager.route('/delete_factory', methods=['POST'])  # noqa: F821
@login_required
@validate_request("llm_factory")
def delete_factory():
    """
    删除指定工厂下的所有LLM配置
    """
    req = request.json
    TenantLLMService.filter_delete(
        [TenantLLM.tenant_id == current_user.id, TenantLLM.llm_factory == req["llm_factory"]])
    return get_json_result(data=True)


@manager.route('/my_llms', methods=['GET'])  # noqa: F821
@login_required
def my_llms():
    """
    获取当前用户已配置的所有LLM信息，按工厂分组，并返回每种模型的类型、名称和已使用的令牌数
    """
    try:
        res = {}
        for o in TenantLLMService.get_my_llms(current_user.id):
            if o["llm_factory"] not in res:
                res[o["llm_factory"]] = {
                    "tags": o["tags"],
                    "llm": []
                }
            res[o["llm_factory"]]["llm"].append({
                "type": o["model_type"],
                "name": o["llm_name"],
                "used_token": o["used_tokens"]
            })
        return get_json_result(data=res)
    except Exception as e:
        return server_error_response(e)


@manager.route('/list', methods=['GET'])  # noqa: F821
@login_required
def list_app():
    """
    列出当前用户可访问的所有LLM模型，包括系统预置模型和用户自定义模型，并支持按模型类型过滤
    """
    self_deployed = ["Youdao", "FastEmbed", "BAAI", "Ollama", "Xinference", "LocalAI", "LM-Studio", "GPUStack"]
    weighted = ["Youdao", "FastEmbed", "BAAI"] if settings.LIGHTEN != 0 else []
    model_type = request.args.get("model_type")
    try:
        objs = TenantLLMService.query(tenant_id=current_user.id)
        facts = set([o.to_dict()["llm_factory"] for o in objs if o.api_key])
        llms = LLMService.get_all()
        llms = [m.to_dict()
                for m in llms if m.status == StatusEnum.VALID.value and m.fid not in weighted]
        for m in llms:
            m["available"] = m["fid"] in facts or m["llm_name"].lower() == "flag-embedding" or m["fid"] in self_deployed

        llm_set = set([m["llm_name"] + "@" + m["fid"] for m in llms])
        for o in objs:
            if o.llm_name + "@" + o.llm_factory in llm_set:
                continue
            llms.append({"llm_name": o.llm_name, "model_type": o.model_type, "fid": o.llm_factory, "available": True})

        res = {}
        for m in llms:
            if model_type and m["model_type"].find(model_type) < 0:
                continue
            if m["fid"] not in res:
                res[m["fid"]] = []
            res[m["fid"]].append(m)

        return get_json_result(data=res)
    except Exception as e:
        return server_error_response(e)
