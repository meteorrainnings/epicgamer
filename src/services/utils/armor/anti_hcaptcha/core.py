import os
import re
import time
from typing import Optional

import requests
from loguru import logger
from selenium.common.exceptions import (
    ElementNotVisibleException,
    WebDriverException,
    TimeoutException,
    ElementClickInterceptedException,
    StaleElementReferenceException,
    NoSuchElementException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait
from undetected_chromedriver import Chrome

from .exceptions import LabelNotFoundException, ChallengeReset, SubmitException
from .solutions import sk_recognition, resnet, yolo


class ArmorCaptcha:
    """hCAPTCHA challenge 驱动控制"""

    # 博大精深！
    label_alias = {
        "自行车": "bicycle",
        "火车": "train",
        "卡车": "truck",
        "公交车": "bus",
        "巴士": "bus",
        "飞机": "airplane",
        "一条船": "boat",
        "船": "boat",
        "摩托车": "motorcycle",
        "垂直河流": "vertical river",
        "天空中向左飞行的飞机": "airplane in the sky flying left",
        "请选择天空中所有向右飞行的飞机": "airplanes in the sky that are flying to the right",
        "汽车": "car",
        "大象": "elephant",
        "鸟": "bird",
        "狗": "dog",
    }

    BAD_CODE = {
        "а": "a",
        "е": "e",
        "e": "e",
        "i": "i",
        "і": "i",
        "ο": "o",
        "с": "c",
        "ԁ": "d",
        "ѕ": "s",
        "һ": "h",
        "ー": "一",
        "土": "士",
    }

    # <success> Challenge Passed by following the expected
    CHALLENGE_SUCCESS = "success"
    # <continue> Continue the challenge
    CHALLENGE_CONTINUE = "continue"
    # <crash> Failure of the challenge as expected
    CHALLENGE_CRASH = "crash"
    # <retry> Your proxy IP may have been flagged
    CHALLENGE_RETRY = "retry"
    # <refresh> Skip the specified label as expected
    CHALLENGE_REFRESH = "refresh"
    # <backcall> (New Challenge) Types of challenges not yet scheduled
    CHALLENGE_BACKCALL = "backcall"

    HOOK_CHALLENGE = "//iframe[contains(@title,'content')]"

    def __init__(
        self,
        dir_workspace: str = None,
        debug: Optional[bool] = False,
        dir_model: str = None,
        screenshot: Optional[bool] = False,
        path_objects_yaml: Optional[str] = None,
        path_rainbow_yaml: Optional[str] = None,
    ):

        self.action_name = "ArmorCaptcha"
        self.debug = debug
        self.dir_model = dir_model
        self.screenshot = screenshot
        self.path_objects_yaml = path_objects_yaml
        self.path_rainbow_yaml = path_rainbow_yaml

        # 存储挑战图片的目录
        self.runtime_workspace = ""
        # 挑战截图存储路径
        self.path_screenshot = ""
        # 样本标签映射 {挑战图片1: locator1, ...}
        self.alias2locator = {}
        # 填充下载链接映射 {挑战图片1: url1, ...}
        self.alias2url = {}
        # 填充挑战图片的缓存地址 {挑战图片1: "/images/挑战图片1.png", ...}
        self.alias2path = {}
        # 存储模型分类结果 {挑战图片1: bool, ...}
        self.alias2answer = {}
        # 图像标签
        self.label = ""
        self.prompt = ""
        # 运行缓存
        self.dir_workspace = dir_workspace if dir_workspace else "."

        # 姿态均衡 超级参数
        self.critical_threshold = 3

        # Automatic registration
        self.pom_handler = resnet.PluggableONNXModels(self.path_objects_yaml)
        self.label_alias.update(self.pom_handler.label_alias["zh"])
        self.pluggable_onnx_models = self.pom_handler.overload(
            self.dir_model, path_rainbow=self.path_rainbow_yaml
        )
        self.yolo_model = yolo.YOLO(self.dir_model)

    def _init_workspace(self):
        """初始化工作目录，存放缓存的挑战图片"""
        _prefix = f"{int(time.time())}" + f"_{self.label}" if self.label else ""
        _workspace = os.path.join(self.dir_workspace, _prefix)
        if not os.path.exists(_workspace):
            os.mkdir(_workspace)
        return _workspace

    def log(self, message: str, **params) -> None:
        """格式化日志信息"""
        if not self.debug:
            return

        motive = "Challenge"
        flag_ = f">> {motive} [{self.action_name}] {message}"
        if params:
            flag_ += " - "
            flag_ += " ".join([f"{i[0]}={i[1]}" for i in params.items()])
        logger.debug(flag_)

    def captcha_screenshot(self, ctx, name_screenshot: str = None):
        """
        保存挑战截图，需要在 get_label 之后执行

        :param name_screenshot: filename of the Challenge image
        :param ctx: Webdriver 或 Element
        :return:
        """
        _suffix = self.label_alias.get(self.label, self.label)
        _filename = (
            f"{int(time.time())}.{_suffix}.png" if name_screenshot is None else name_screenshot
        )
        _out_dir = os.path.join(os.path.dirname(self.dir_workspace), "captcha_screenshot")
        _out_path = os.path.join(_out_dir, _filename)
        os.makedirs(_out_dir, exist_ok=True)

        # FullWindow screenshot or FocusElement screenshot
        try:
            ctx.screenshot(_out_path)
        except AttributeError:
            ctx.save_screenshot(_out_path)
        except Exception as err:
            self.log("挑战截图保存失败，错误的参数类型", type=type(ctx), err=err)
        finally:
            return _out_path

    def switch_solution(self):
        """Optimizing solutions based on different challenge labels"""
        sk_solution = {
            "vertical river": sk_recognition.VerticalRiverRecognition,
            "airplane in the sky flying left": sk_recognition.LeftPlaneRecognition,
            "airplanes in the sky that are flying to the right": sk_recognition.RightPlaneRecognition,
        }

        label_alias = self.label_alias.get(self.label)

        # Select ResNet ONNX model
        if self.pluggable_onnx_models.get(label_alias):
            return self.pluggable_onnx_models[label_alias]
        # Select SK-Image method
        if sk_solution.get(label_alias):
            return sk_solution[label_alias](self.path_rainbow_yaml)
        # Select YOLO ONNX model
        return self.yolo_model

    def mark_samples(self, ctx: Chrome):
        """
        获取每个挑战图片的下载链接以及网页元素位置

        :param ctx:
        :return:
        """
        # 等待图片加载完成
        WebDriverWait(ctx, 25, ignored_exceptions=ElementNotVisibleException).until(
            EC.presence_of_all_elements_located((By.XPATH, "//div[@class='task-image']"))
        )
        time.sleep(0.3)

        # DOM 定位元素
        samples = ctx.find_elements(By.XPATH, "//div[@class='task-image']")
        for sample in samples:
            alias = sample.get_attribute("aria-label")
            while True:
                try:
                    image_style = sample.find_element(By.CLASS_NAME, "image").get_attribute("style")
                    url = re.split(r'[(")]', image_style)[2]
                    self.alias2url.update({alias: url})
                    break
                except IndexError:
                    continue
            self.alias2locator.update({alias: sample})

    def get_label(self, ctx: Chrome):
        """获取人机挑战需要识别的图片类型（标签）"""

        def split_prompt_message(prompt_message: str, _lang: str = "zh") -> str:
            """根据指定的语种在提示信息中分离挑战标签"""
            labels_mirror = {
                "zh": re.split(r"[包含 图片]", prompt_message)[2][:-1]
                if "包含" in prompt_message
                else prompt_message,
                "en": re.split(r"containing a", prompt_message)[-1][1:].strip().replace(".", "")
                if "containing" in prompt_message
                else prompt_message,
            }
            return labels_mirror[_lang]

        def label_cleaning(raw_label: str) -> str:
            """清洗误码 | 将不规则 UNICODE 字符替换成正常的英文字符"""
            clean_label = raw_label
            for c in self.BAD_CODE:
                clean_label = clean_label.replace(c, self.BAD_CODE[c])
            return clean_label

        try:
            time.sleep(1)
            label_obj = WebDriverWait(ctx, 30, ignored_exceptions=ElementNotVisibleException).until(
                EC.presence_of_element_located((By.XPATH, "//h2[@class='prompt-text']"))
            )
        except TimeoutException:
            raise ChallengeReset("人机挑战意外通过")
        else:
            try:
                self.prompt = label_obj.text
                _label = split_prompt_message(prompt_message=self.prompt)
            except (AttributeError, IndexError):
                raise LabelNotFoundException("获取到异常的标签对象。")
            else:
                self.label = label_cleaning(_label)
                self.log(
                    message="获取挑战标签", label=f"「{self.label_alias.get(self.label, self.label)}」"
                )

    def tactical_retreat(self, ctx) -> Optional[str]:
        """模型存在泛化死角，遇到指定标签时主动进入下一轮挑战，节约时间"""
        if self.label_alias.get(self.label):
            return self.CHALLENGE_CONTINUE

        # 保存挑战截图 | 返回截图存储路径
        try:
            challenge_container = ctx.find_element(By.XPATH, "//body[@class='no-selection']")
            self.path_screenshot = self.captcha_screenshot(challenge_container)
        except NoSuchElementException:
            pass
        except WebDriverException as err:
            logger.exception(err)
        finally:
            q = self.label.replace(" ", "+")
            self.log(
                "Types of challenges not yet scheduled",
                label=f"「{self.label}」",
                prompt=f"「{self.prompt}」",
                screenshot=self.path_screenshot,
                site_link=ctx.current_url,
                issues=f"https://github.com/QIN2DIM/hcaptcha-challenger/issues?q={q}",
            )
            return self.CHALLENGE_BACKCALL

    def download_images(self):
        """
        下载挑战图片

        ### hcaptcha 设有挑战时长的限制

          如果一段时间内没有操作页面元素，<iframe> 框体就会消失，之前获取的 Element Locator 将过时。
          需要借助一些现代化的方法尽可能地缩短 `获取数据集` 的耗时。

        ### 解决方案

        1. 使用基于协程的方法拉取图片到本地，最佳实践（本方法）。拉取效率比遍历下载提升至少 10 倍。
        2. 截屏切割，有一定的编码难度。直接截取目标区域的九张图片，使用工具函数切割后识别。需要自己编织定位器索引。

        :return:
        """
        _workspace = self._init_workspace()
        for alias, url in self.alias2url.items():
            path_challenge_img = os.path.join(_workspace, f"{alias}.png")
            stream = requests.get(url).content
            with open(path_challenge_img, "wb") as file:
                file.write(stream)

    def challenge(self, ctx: Chrome, model):
        """
        图像分类，元素点击，答案提交

        ### 性能瓶颈

        此部分图像分类基于 CPU 运行。如果服务器资源极其紧张，图像分类任务可能无法按时完成。
        根据实验结论来看，如果运行时内存少于 512MB，且仅有一个逻辑线程的话，基本上是与深度学习无缘了。

        ### 优雅永不过时

        `hCaptcha` 的挑战难度与 `reCaptcha v2` 不在一个级别。
        这里只要正确率上去就行，也即正确图片覆盖更多，通过率越高（即使因此多点了几个干扰项也无妨）。
        所以这里要将置信度尽可能地调低（未经针对训练的模型本来就是用来猜的）。

        :return:
        """
        self.log(message="开始挑战")

        ta = []
        # {{< IMAGE CLASSIFICATION >}}
        for alias in self.alias2path.keys():
            # Read binary data weave into types acceptable to the model
            with open(self.alias2path[alias], "rb") as file:
                data = file.read()
            # Get detection results
            t0 = time.time()
            result = model.solution(img_stream=data, label=self.label_alias[self.label])
            ta.append(time.time() - t0)
            # Pass: Hit at least one object
            if result:
                try:
                    self.alias2locator[alias].click()
                except StaleElementReferenceException:
                    pass
                except WebDriverException as err:
                    logger.warning(err)

        # Check result of the challenge.
        _filename = f"{int(time.time())}.{model.flag}.{self.label_alias[self.label]}.png"
        self.captcha_screenshot(ctx, name_screenshot=_filename)

        # {{< SUBMIT ANSWER >}}
        try:
            WebDriverWait(ctx, 35, ignored_exceptions=ElementClickInterceptedException).until(
                EC.element_to_be_clickable((By.XPATH, "//div[@class='button-submit button']"))
            ).click()
        except ElementClickInterceptedException:
            pass
        except WebDriverException as err:
            self.log("挑战提交失败", err=err)
            raise SubmitException from err
        else:
            self.log(message=f"提交挑战 {model.flag}: {round(sum(ta), 2)}s")

    def challenge_success(self, ctx: Chrome, **kwargs):
        """
        自定义的人机挑战通过逻辑

        :return:
        """

    def anti_checkbox(self, ctx):
        """处理复选框"""
        for _ in range(8):
            try:
                # [👻] 进入复选框
                WebDriverWait(ctx, 2, ignored_exceptions=ElementNotVisibleException).until(
                    EC.frame_to_be_available_and_switch_to_it(
                        (By.XPATH, "//div[@id='cf-hcaptcha-container']//div[not(@style)]//iframe")
                    )
                )
                # [👻] 点击复选框
                WebDriverWait(ctx, 2).until(EC.element_to_be_clickable((By.ID, "checkbox"))).click()
                self.log("Handle hCaptcha checkbox")
                return True
            except ElementClickInterceptedException:
                return False
            except TimeoutException:
                pass
            finally:
                # [👻] 回到主线剧情
                ctx.switch_to.default_content()

    def anti_captcha(self):
        """
        自定义的人机挑战触发逻辑

        具体思路是：
        1. 进入 hcaptcha iframe
        2. 获取图像标签
            需要加入判断，有时候 `hcaptcha` 计算的威胁程度极低，会直接让你过，
            于是图像标签之类的元素都不会加载在网页上。
        3. 获取各个挑战图片的下载链接及网页元素位置
        4. 图片下载，分类
            需要用一些技术手段缩短这部分操作的耗时。人机挑战有时间限制。
        5. 对正确的图片进行点击
        6. 提交答案
        7. 判断挑战是否成功
            一般情况下 `hcaptcha` 的验证有两轮，
            而 `recaptcha vc2` 之类的人机挑战就说不准了，可能程序一晚上都在“循环”。
        :return:
        """
