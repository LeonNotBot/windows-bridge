import copy
import hashlib
import io
from zipfile import ZipFile

from fastapi.testclient import TestClient

from bridge import create_app
from config import mock_targets
from test_report_mode import HEADERS, wait


def test_review_report_operation_accepts_only_registered_workbook_and_reuses_job(tmp_path):
    with TestClient(create_app('t'*32, targets=mock_targets(), data_dir=tmp_path), headers=HEADERS) as client:
        generation = {'idempotency_key':'visual-generation', 'target_id':'mock-visual-review',
                      'input':{'benchmark_id':'smoke'}}
        job = client.post('/v1/jobs',json=generation).json()['job_id']
        assert wait(client,job)['status'] == 'succeeded'
        result = client.get(f'/v1/jobs/{job}/result').json()['output']
        assert result['requires_review'] and result['review_report_supported']
        source = next(f for f in result['files'] if f['name'].endswith('.xlsx'))
        original = client.get(source['download_url']).content
        scored = io.BytesIO()
        with ZipFile(io.BytesIO(original)) as archive, ZipFile(scored,'w') as target:
            for item in archive.infolist():
                raw = archive.read(item.filename)
                if item.filename == 'xl/worksheets/sheet3.xml':
                    for key,value in [('H5','满足'),('H6','不满足'),('I7','L2')]:
                        raw = raw.replace(f'<c r="{key}" t="inlineStr"><is><t></t></is></c>'.encode(),
                                          f'<c r="{key}" t="inlineStr"><is><t>{value}</t></is></c>'.encode())
                target.writestr(copy.copy(item),raw)
        uploaded = client.post('/v1/artifacts',files={'file':('reviewed.xlsx',scored.getvalue(),source['content_type'])})
        assert uploaded.status_code == 201
        artifact = uploaded.json()
        request = {'idempotency_key':'visual-final-report','target_id':'mock-visual-review',
                   'operation':'finalize_report','input':{'benchmark_id':'smoke','source_job_id':job},
                   'input_artifacts':[{k:artifact[k] for k in ('artifact_id','sha256')}]}
        invalid = copy.deepcopy(request)
        invalid['input']['source_job_id'] = '0'*32
        assert client.post('/v1/jobs',json=invalid).status_code == 422
        invalid = copy.deepcopy(request)
        invalid['input']['benchmark_id'] = 'two-samples'
        assert client.post('/v1/jobs',json=invalid).status_code == 422
        invalid = copy.deepcopy(request)
        invalid['input_artifacts'][0]['sha256'] = '0'*64
        assert client.post('/v1/jobs',json=invalid).status_code == 422
        invalid = {**generation, 'idempotency_key':'injection', 'input_artifacts':request['input_artifacts']}
        assert client.post('/v1/jobs',json=invalid).status_code == 422
        final = client.post('/v1/jobs',json=request)
        assert final.status_code == 202,final.text
        final_id = final.json()['job_id']
        assert wait(client,final_id)['status'] == 'succeeded'
        assert client.post('/v1/jobs',json=request).json()['job_id'] == final_id
        output = client.get(f'/v1/jobs/{final_id}/result').json()['output']
        main = next(f for f in output['files'] if f['artifact_id']==output['main_artifact_id'])
        assert '不满足' in client.get(main['download_url']).text
        assert not output['requires_review']
        assert hashlib.sha256(client.get(source['download_url']).content).hexdigest() == source['sha256']
