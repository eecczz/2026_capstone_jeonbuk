import jpype
import jpype.imports
from jpype.types import JString
import tempfile
import os
import logging
import threading
from typing import List
from langchain_core.documents import Document
from open_webui.env import GLOBAL_LOG_LEVEL

log = logging.getLogger(__name__)
log.setLevel(GLOBAL_LOG_LEVEL)


class JVMManager:
    """전역 JVM 관리자 - 스레드 안전한 JVM 생명주기 관리"""
    _instance = None
    _lock = threading.Lock()
    _jvm_started = False
    _hwp_jar_path = None
    _hwpx_jar_path = None
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(JVMManager, cls).__new__(cls)
        return cls._instance
    
    def initialize(self, hwp_jar_path, hwpx_jar_path):
        """JVM 초기화 (한 번만 실행)"""
        with self._lock:
            if not self._jvm_started:
                self._hwp_jar_path = hwp_jar_path
                self._hwpx_jar_path = hwpx_jar_path
                
                if not jpype.isJVMStarted():
                    try:
                        jpype.startJVM(
                            jpype.getDefaultJVMPath(),
                            f"-Djava.class.path={hwp_jar_path}:{hwpx_jar_path}"
                        )
                        self._jvm_started = True
                        log.info("JVM이 성공적으로 시작되었습니다.")
                    except Exception as e:
                        log.error(f"JVM 시작 실패: {str(e)}")
                        raise
                else:
                    self._jvm_started = True
                    log.info("JVM이 이미 실행 중입니다.")
    
    def is_initialized(self):
        """JVM이 초기화되었는지 확인"""
        return self._jvm_started and jpype.isJVMStarted()


class HWPProcessor:
    def __init__(self, hwp_jar_path, hwpx_jar_path):
        self.hwp_jar_path = hwp_jar_path
        self.hwpx_jar_path = hwpx_jar_path
        self.jvm_manager = JVMManager()
    
    def ensure_jvm(self):
        """JVM이 실행 중인지 확인하고 필요시 시작"""
        if not self.jvm_manager.is_initialized():
            self.jvm_manager.initialize(self.hwp_jar_path, self.hwpx_jar_path)
    
    def extract_hwp_text(self, file_path):
        """HWP 파일에서 텍스트 추출"""
        try:
            self.ensure_jvm()
            
            from kr.dogfoot.hwplib.reader import HWPReader
            from kr.dogfoot.hwplib.tool.textextractor import TextExtractor
            # from kr.dogfoot.hwplib.tool.textextractor.TextExtractMethod import TextExtractMethod
            from java.io import File
            
            ## hwp 파싱 오류 수정
            TextExtractMethod = jpype.JClass('kr.dogfoot.hwplib.tool.textextractor.TextExtractMethod')
            
            java_file = File(file_path)
            hwp_file = HWPReader.fromFile(java_file)
            if hwp_file is not None:
                text = TextExtractor.extract(
                    hwp_file, 
                    TextExtractMethod.InsertControlTextBetweenParagraphText
                )
                return str(text)
            return ""
            
        except Exception as e:
            log.error(f"HWP 처리 오류: {str(e)}")
            return f"HWP 처리 오류: {str(e)}"
    
    def extract_hwpx_text(self, file_path):
        """HWPX 파일에서 텍스트 추출"""
        try:
            self.ensure_jvm()
            
            from kr.dogfoot.hwpxlib.reader import HWPXReader
            from kr.dogfoot.hwpxlib.tool.textextractor import TextExtractor as HWPXTextExtractor
            from java.io import File
            
            java_file = File(file_path)
            hwpx_file = HWPXReader.fromFile(java_file)
            if hwpx_file is not None:
                try:
                    text = HWPXTextExtractor.extract(hwpx_file)
                except:
                    text = HWPXTextExtractor.extract(hwpx_file, None, True, None)
                
                return str(text)
            return ""
            
        except Exception as e:
            log.error(f"HWPX 처리 오류: {str(e)}")
            return f"HWPX 처리 오류: {str(e)}"


class HWPLoader:
    """HWP/HWPX 파일을 처리하는 LangChain 로더"""
    
    def __init__(self, file_path: str, hwp_jar_path: str = None, hwpx_jar_path: str = None):
        self.file_path = file_path
        self.hwp_jar_path = hwp_jar_path or os.getenv('HWP_JAR_PATH', '/app/backend/python-hwplib/hwplib-1.1.8.jar')
        self.hwpx_jar_path = hwpx_jar_path or os.getenv('HWPX_JAR_PATH', '/app/backend/python-hwpxlib/hwpxlib-1.0.5.jar')
        self.processor = None
    
    def load(self) -> List[Document]:
        """HWP/HWPX 파일을 로드하고 Document 객체로 변환"""
        file_extension = os.path.splitext(self.file_path)[1].lower()
        
        # JAR 파일 존재 여부 확인
        if file_extension == '.hwp' and not os.path.exists(self.hwp_jar_path):
            log.error(f"HWP JAR 파일을 찾을 수 없습니다: {self.hwp_jar_path}")
            return [Document(
                page_content="HWP 파일 처리를 위한 JAR 파일이 설치되지 않았습니다. 관리자에게 문의하세요.",
                metadata={
                    "source": self.file_path,
                    "error": "jar_file_not_found",
                    "processing_engine": "hwp_processor"
                }
            )]
        
        if file_extension == '.hwpx' and not os.path.exists(self.hwpx_jar_path):
            log.error(f"HWPX JAR 파일을 찾을 수 없습니다: {self.hwpx_jar_path}")
            return [Document(
                page_content="HWPX 파일 처리를 위한 JAR 파일이 설치되지 않았습니다. 관리자에게 문의하세요.",
                metadata={
                    "source": self.file_path,
                    "error": "jar_file_not_found",
                    "processing_engine": "hwp_processor"
                }
            )]
        
        self.processor = HWPProcessor(self.hwp_jar_path, self.hwpx_jar_path)
        
        try:
            if file_extension == '.hwp':
                text_content = self.processor.extract_hwp_text(self.file_path)
            elif file_extension == '.hwpx':
                text_content = self.processor.extract_hwpx_text(self.file_path)
            else:
                raise ValueError(f"지원하지 않는 파일 형식: {file_extension}")
            
            # 텍스트가 비어있거나 오류 메시지인 경우 처리
            if not text_content or text_content.startswith("HWP 처리 오류") or text_content.startswith("HWPX 처리 오류"):
                log.warning(f"텍스트 추출 실패: {text_content}")
                text_content = "텍스트를 추출할 수 없습니다."
            
            # Document 객체 생성
            metadata = {
                "source": self.file_path,
                "file_type": file_extension[1:],  # .hwp -> hwp
                "processing_engine": "hwp_processor"
            }
            
            return [Document(page_content=text_content, metadata=metadata)]
            
        except Exception as e:
            log.error(f"HWP/HWPX 파일 처리 중 오류 발생: {str(e)}")
            return [Document(
                page_content=f"파일 처리 중 오류가 발생했습니다: {str(e)}",
                metadata={
                    "source": self.file_path,
                    "error": str(e),
                    "processing_engine": "hwp_processor"
                }
            )]


def process_hwp_hwpx_files(file_path, file_extension):
    """HWP/HWPX 파일 처리 메인 함수 (기존 코드와의 호환성을 위해 유지)"""
    
    processor = HWPProcessor(
        os.getenv('HWP_JAR_PATH', '/app/backend/python-hwplib/hwplib-1.1.8.jar'),
        os.getenv('HWPX_JAR_PATH', '/app/backend/python-hwpxlib/hwpxlib-1.0.5.jar')
    )
    
    try:
        if file_extension.lower() == '.hwp':
            return processor.extract_hwp_text(file_path)
        elif file_extension.lower() == '.hwpx':
            return processor.extract_hwpx_text(file_path)
        else:
            return "지원하지 않는 파일 형식입니다."
    except Exception as e:
        log.error(f"파일 처리 중 오류: {str(e)}")
        return f"파일 처리 중 오류가 발생했습니다: {str(e)}" 
