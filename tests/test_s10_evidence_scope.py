"""A hash-correct irrelevant artifact is not independent product proof."""
import hashlib
import importlib.util
import json
import pytest
from pathlib import Path


@pytest.mark.parametrize('all_records',[False,True])
def test_structural_pass_keeps_semantic_and_loaded_client_claims_unestablished(tmp_path,all_records):
    path=Path(__file__).resolve().parents[1]/'scripts/author_requirements_eval.py'
    spec=importlib.util.spec_from_file_location('s10_requirements',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    artifact=tmp_path/'unrelated.txt';artifact.write_bytes(b'An irrelevant hash-correct artifact. No client ran.\n')
    evidence=tmp_path/'claimed.json'
    row={'status':'PASS','commit':module.current_commit(),'command':'fixture declaration only',
        'execution_surface':'ordinary_chat','profile_id':'declared-not-observed',
        'artifacts':[{'path':str(artifact),'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest()}]}
    register,_=module.load_register()
    runs={source['id']:row for source in register} if all_records else {'T01':row}
    evidence.write_text(json.dumps({'schema_version':1,'runs':runs}))
    result=module.evaluate(evidence)
    assert result['scenarios'][0]['evaluated_status']=='PASS'
    assert result['scenarios'][0]['status']=='NOT_RUN_IN_THIS_RESEARCH'
    assert result['scenarios'][0]['evaluated_status_scope']=='evidence_record_validation'
    assert result['independent_semantic_acceptance'] is False
    assert result['claim_relevance_review']=='required_separately'
    assert result['candidate_binding']=='git_head_only'
    assert result['installed_or_loaded_bytes_observed'] is False
    assert result['client_observation_performed_by_evaluator'] is False
    assert result['counts']['NOT_RUN']==(0 if all_records else 51)
    assert result['counts']['PASS']==(52 if all_records else 1)
    assert result['overall']==('PASS' if all_records else 'INCOMPLETE')
    assert all(r['status']=='NOT_RUN_IN_THIS_RESEARCH' for r in result['scenarios'])
    # Retain both the validator's real result and the separate bounded review.
    (tmp_path/'structural-result.json').write_text(json.dumps(result,indent=2))
    (tmp_path/'claim-adjudication.json').write_text(json.dumps({
        'claim':'All 52 operational cases executed and accepted' if all_records else 'Actual ordinary-Pro T01 read path qualified',
        'decision':'REJECTED_PRODUCT_CLAIM','adjudicator':'explicit integration review',
        'basis':'The controlled artifact contains no request, ingress, rendered content or actual-client witness. Caller labels and matching hashes prove none of these.',
        'structural_result':'PASS for supplied records only','actual_execution':'local no-client fixture',
        'original_T01':'NOT_RUN','universal_semantic_judge_added':False},indent=2))
