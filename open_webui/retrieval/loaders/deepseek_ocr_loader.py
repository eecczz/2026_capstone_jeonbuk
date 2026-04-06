"""
DeepSeek OCR Loader for LangChain
PyPDFLoader와 동일한 인터페이스를 제공하는 OCR 로더
PDF와 이미지 파일을 모두 처리할 수 있습니다.
"""
import requests
import os
from typing import List, Optional
from langchain_core.documents import Document
from langchain_community.document_loaders.base import BaseLoader
from pdf2image import convert_from_path
from PIL import Image
import io


class DeepSeekOCRLoader(BaseLoader):
    """
    DeepSeek OCR 서버를 사용하여 PDF 또는 이미지를 OCR 처리하고 Document를 반환하는 로더
    
    PyPDFLoader와 동일한 방식으로 사용 가능:
        loader = DeepSeekOCRLoader(
            file_path="document.pdf",  # 또는 "image.png"
            ocr_server_url="http://localhost:8000",
            extract_images=True  # PyPDFLoader 호환을 위한 옵션
        )
        documents = loader.load()
    """
    
    def __init__(
        self,
        file_path: str,
        ocr_server_url: str = "http://localhost:8000",
        extract_images: Optional[bool] = None,
        **kwargs
    ):
        """
        Args:
            file_path: OCR 처리할 PDF 또는 이미지 파일 경로
            ocr_server_url: OCR 서버 URL (기본값: http://localhost:8000)
            extract_images: PyPDFLoader 호환을 위한 옵션 (PDF의 경우 이미지 추출 여부)
            **kwargs: 추가 옵션
        """
        self.file_path = file_path
        self.ocr_server_url = ocr_server_url.rstrip("/")
        self.extract_images = extract_images
        self.kwargs = kwargs
    
    def _is_pdf(self, file_path: str) -> bool:
        """파일이 PDF인지 확인"""
        return file_path.lower().endswith(".pdf")
    
    def _is_image(self, file_path: str) -> bool:
        """파일이 이미지인지 확인"""
        image_extensions = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".webp")
        return file_path.lower().endswith(image_extensions)

    def _is_hwp(self, file_path: str) -> bool:
        """파일이 HWP/HWPX인지 확인"""
        return file_path.lower().endswith((".hwp", ".hwpx"))
    
    def _ocr_image(self, image: Image.Image, filename: str, page_num: Optional[int] = None) -> Document:
        """단일 이미지를 OCR 처리하여 Document 반환"""
        try:
            # 이미지를 바이트로 변환
            img_byte_arr = io.BytesIO()
            image.save(img_byte_arr, format="PNG")
            img_byte_arr.seek(0)
            
            # OCR 서버로 전송
            files = {"file": (filename, img_byte_arr, "image/png")}
            response = requests.post(
                f"{self.ocr_server_url}/ocr",
                files=files,
                timeout=300
            )
            response.raise_for_status()
            result = response.json()
            
            # Document로 변환
            doc_data = result.get("documents", [{}])[0]
            metadata = {
                **doc_data.get("metadata", {}),
                "file_path": self.file_path,
                "extract_images": self.extract_images
            }
            
            # PDF인 경우 페이지 번호 추가
            if page_num is not None:
                metadata["page"] = page_num
            
            document = Document(
                page_content=doc_data.get("page_content", ""),
                metadata=metadata
            )
            
            return document
            
        except requests.exceptions.RequestException as e:
            raise Exception(f"OCR server request failed: {str(e)}")
        except Exception as e:
            raise Exception(f"Failed to process image: {str(e)}")
    
    def load(self) -> List[Document]:
        """
        PDF 또는 이미지를 OCR 처리하여 Document 리스트를 반환
        
        Returns:
            List[Document]: OCR 처리된 텍스트를 포함한 Document 리스트
            - PDF의 경우: 각 페이지별로 Document 생성 (PyPDFLoader와 동일)
            - 이미지의 경우: 단일 Document 반환
        """
        if not os.path.exists(self.file_path):
            raise FileNotFoundError(f"File not found: {self.file_path}")
        
        documents = []
        
        # PDF 파일 처리
        if self._is_pdf(self.file_path):
            try:
                # PDF를 이미지로 변환 (각 페이지별)
                images = convert_from_path(self.file_path)
                
                # 각 페이지를 OCR 처리
                for page_num, image in enumerate(images, start=1):
                    document = self._ocr_image(
                        image, 
                        f"{os.path.basename(self.file_path)}_page_{page_num}.png",
                        page_num=page_num
                    )
                    # PyPDFLoader와 동일한 형식으로 metadata 설정
                    document.metadata.update({
                        "source": self.file_path,
                        "page": page_num,
                        "total_pages": len(images)
                    })
                    documents.append(document)
                
            except Exception as e:
                raise Exception(f"Failed to process PDF: {str(e)}")
        
        # 이미지 파일 처리
        elif self._is_image(self.file_path):
            try:
                image = Image.open(self.file_path).convert("RGB")
                document = self._ocr_image(
                    image,
                    os.path.basename(self.file_path),
                    page_num=None
                )
                documents.append(document)
                
            except Exception as e:
                raise Exception(f"Failed to process image: {str(e)}")
        
        # HWP/HWPX 파일 처리
        elif self._is_hwp(self.file_path):
            try:
                with open(self.file_path, "rb") as f:
                    files = {"file": (os.path.basename(self.file_path), f)}
                    response = requests.post(
                        f"{self.ocr_server_url}/ocr/hwp",
                        files=files,
                        timeout=600
                    )
                    response.raise_for_status()
                    result = response.json()

                for doc_data in result.get("documents", []):
                    metadata = {
                        **doc_data.get("metadata", {}),
                        "file_path": self.file_path,
                        "source": self.file_path
                    }
                    document = Document(
                        page_content=doc_data.get("page_content", ""),
                        metadata=metadata
                    )
                    documents.append(document)

            except requests.exceptions.RequestException as e:
                raise Exception(f"OCR server request failed: {str(e)}")
            except Exception as e:
                raise Exception(f"Failed to process HWP: {str(e)}")

        else:
            raise ValueError(
                f"Unsupported file type: {self.file_path}. "
                f"Supported formats: PDF (.pdf), HWP (.hwp, .hwpx) and images (.png, .jpg, .jpeg, etc.)"
            )

        return documents

    def load_as_text(self) -> dict:
        """
        파일을 OCR 처리하여 텍스트 형식으로 반환 (HWP/HWPX, PDF만 지원)

        Returns:
            dict: {"text": str, "metadata": dict, "pages": list}
        """
        if not os.path.exists(self.file_path):
            raise FileNotFoundError(f"File not found: {self.file_path}")

        if self._is_hwp(self.file_path):
            endpoint = "/text/hwp"
        elif self._is_pdf(self.file_path):
            endpoint = "/text/pdf"
        else:
            raise ValueError(
                f"load_as_text only supports PDF and HWP files. "
                f"Got: {self.file_path}"
            )

        try:
            with open(self.file_path, "rb") as f:
                files = {"file": (os.path.basename(self.file_path), f)}
                response = requests.post(
                    f"{self.ocr_server_url}{endpoint}",
                    files=files,
                    timeout=600
                )
                response.raise_for_status()
                return response.json()

        except requests.exceptions.RequestException as e:
            raise Exception(f"OCR server request failed: {str(e)}")
