# bblove-law-open

법제처 국가법령정보 공동활용 OPEN API(DRF)로 대한민국 현행 법령 원문을 수집해 검증한 뒤 GitHub Release로 게시하는 공개 저장소입니다.

- **출처**: 법제처 국가법령정보 공동활용(law.go.kr DRF). 법령·판결 등은 저작권법 제7조의 보호받지 못하는 저작물입니다.
- **게시물**: Releases의 `law-YYYYMMDD-<run_id>` 태그. 각 게시에는 `release-info.json`(형식 버전·출처 커밋·파일별 SHA-256), `SHA256SUMS`, GitHub 빌드 출처 증명(attestation)이 붙습니다.
- **검증**: 수집 후 조문 수·식별자·시행일 검사와 자산 빌드 검사를 통과한 수집본만 게시합니다. 실패 목록(`collection-failures.json`)도 함께 게시합니다.
- **범위 밖**: 이 저장소는 수집 코드와 공공 법령 데이터만 다룹니다. 개인 정보, 질의 내용, 인증값은 넣지 않습니다.

공식 원문 확인은 항상 [국가법령정보센터](https://www.law.go.kr)에서 하십시오.
