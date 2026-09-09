import pytest
from review_writer_core.bibliography_audit import audit_bibliography, _provider_exclusion_reason
from review_writer_core.paper_sources.base import SourceSearchResult
from review_writer_core.publication_voice import normalize_publication_voice, publication_voice_issues

META={'title':{'value':'A general synthesis of chiral molecules','confidence':.95},'authors':{'value':['A Writer'],'confidence':.95},'doi':{'value':'10.1000/original','confidence':1.0},'year':{'value':2013,'confidence':.9}}
BAD={'title':'ChemInform Abstract: A general synthesis of chiral molecules.','authors':['B Writer'],'year':2014,'identifiers':{'doi':'10.1000/abstract'}}

def test_unrelated_cached_record_does_not_create_field_conflicts():
    previous={'sources':{'crossref':{'status':'conflict','candidate':BAD,'match':{'title_similarity':.9}}}}
    result=audit_bibliography(META,connectors=[],previous_audit=previous,network_mode='force')
    assert result['sources']['crossref']['status']=='not_same_publication'
    assert not any(row['source']=='crossref' for rows in result['field_provenance'].values() for row in rows)
    assert previous['sources']['crossref']['status']=='conflict'

@pytest.mark.parametrize('prefix',['ChemInform Abstract: ','Abstract: ','Correction to: ','Erratum: '])
def test_secondary_record_without_doi_is_excluded(prefix):
    assert _provider_exclusion_reason(META,{'title':prefix+META['title']['value']})=='secondary_publication_record'

def test_primary_candidate_survives_secondary_search_result():
    class Connector:
        name='crossref'
        def search(self, request):
            return SourceSearchResult(source=self.name,status='completed',candidates=[BAD,{'title':META['title']['value'],'authors':['A Writer'],'year':2013,'identifiers':{'doi':'10.1000/original'}}])
    result=audit_bibliography(META,connectors=[Connector()],network_mode='force')
    assert result['sources']['crossref']['status']=='verified'
    assert len(result['sources']['crossref']['excluded_candidates'])==1

def test_exact_doi_keeps_real_field_conflicts_visible():
    assert _provider_exclusion_reason(META,{**BAD,'identifiers':{'doi':'10.1000/original'}})==''

def test_voice_normalization_preserves_comparison_limit_and_citations():
    source='Results cannot be ranked because the supplied passages identify different studies and substrate sets. [3, 14]'
    result=normalize_publication_voice(source)
    assert result=='Results cannot be ranked because the cited sources identify different studies and substrate sets. [3, 14]'
    assert not publication_voice_issues(result)
    assert normalize_publication_voice(result)==result

def test_voice_normalization_does_not_hide_missing_evidence_or_change_quotes():
    source='No result is available in the supplied passages.\n> The supplied passages identify two studies.\n"supplied passages identify"\n`supplied passages identify`\n<!-- supplied passages identify -->\n## References\n[1] Supplied passages identify research.'
    assert normalize_publication_voice(source)==source

def test_metadata_reaudit_version_is_not_a_content_change():
    from copy import deepcopy
    from review_writer_api.domain_services.final import _same_metadata_content
    before={'title':{'value':'Study'},'_artifact_ids':{'metadata':'old','pdf':'source'},'_artifact_paths':{'metadata':'old.json','pdf':'source.pdf'}}
    after=deepcopy(before); after['_artifact_ids']['metadata']='new'; after['_artifact_paths']['metadata']='new.json'
    assert _same_metadata_content(before,after)
    after['title']['value']='Different study'
    assert not _same_metadata_content(before,after)
    after['title']['value']='Study'; after['_artifact_ids']['pdf']='different-source'
    assert not _same_metadata_content(before,after)
