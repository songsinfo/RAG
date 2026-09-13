package dev.rag.knowledge;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import org.junit.jupiter.api.*;
import org.springframework.jdbc.datasource.DriverManagerDataSource;
import org.springframework.jdbc.datasource.DataSourceTransactionManager;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;
class DeletionPropagationTest {
 Db db;DeletionPropagation deletion;
 @BeforeEach void setup(){var ds=new DriverManagerDataSource("jdbc:h2:mem:"+UUID.randomUUID()+";MODE=MySQL;DB_CLOSE_DELAY=-1","sa","");db=new Db(new NamedParameterJdbcTemplate(ds));String[] schema={
 "document(tenant_id VARCHAR,id VARCHAR,status VARCHAR,lifecycle_epoch INT,active_build_id VARCHAR,title VARCHAR)",
 "eval_snapshot_document(tenant_id VARCHAR,document_id VARCHAR,snapshot_id VARCHAR,origin_system VARCHAR,origin_tenant_id VARCHAR,origin_document_id VARCHAR)",
 "eval_snapshot(tenant_id VARCHAR,id VARCHAR,status VARCHAR,manifest_json JSON)",
 "artifact(tenant_id VARCHAR,document_id VARCHAR,storage_key VARCHAR,status VARCHAR)",
 "qa_run(tenant_id VARCHAR,id VARCHAR,segment_id VARCHAR,turn_no INT,conversation_id VARCHAR,question VARCHAR,result_json JSON,status VARCHAR,terminal_reason VARCHAR)",
 "run_source(tenant_id VARCHAR,run_id VARCHAR,document_id VARCHAR,span_json JSON)",
 "source_unit(tenant_id VARCHAR,document_id VARCHAR,text_content VARCHAR,location JSON)",
 "chunk(tenant_id VARCHAR,build_id VARCHAR,text_content VARCHAR,content_hash BINARY)",
 "index_build(tenant_id VARCHAR,id VARCHAR,document_id VARCHAR,quality_warnings JSON,review_reason VARCHAR)",
 "representation(tenant_id VARCHAR,build_id VARCHAR,text_content VARCHAR,vector_bytes BINARY,dims INT,vector_hash BINARY,text_hash BINARY)",
 "document_version(tenant_id VARCHAR,document_id VARCHAR,original_name VARCHAR)",
 "eval_gold(tenant_id VARCHAR,case_id VARCHAR,origin_system VARCHAR,origin_tenant_id VARCHAR,origin_document_id VARCHAR,anchor_json JSON)",
 "eval_case(tenant_id VARCHAR,id VARCHAR,dataset_version_id VARCHAR,question_json JSON,expected_json JSON,annotation_status VARCHAR)",
 "eval_dataset_version(tenant_id VARCHAR,id VARCHAR,status VARCHAR)",
 "eval_review(tenant_id VARCHAR,result_id VARCHAR,labels_json JSON,reason_text VARCHAR)",
 "eval_result(tenant_id VARCHAR,id VARCHAR,case_id VARCHAR,qa_run_id VARCHAR,metrics_json JSON,judge_json JSON,status VARCHAR)"};for(String ddl:schema)db.exec("CREATE TABLE "+ddl,Map.of());deletion=new DeletionPropagation(db,new AccessPolicy(db),new TransactionTemplate(new DataSourceTransactionManager(ds)),mock(PrivateArtifacts.class),mock(IndexWriter.class),"","","evaluation");}
 private void seed(String source,String suffix){var p=Db.params("s",source,"x",suffix);db.exec("INSERT INTO document VALUES ('tenant',:x,'LIVE',0,'build','secret')",p);db.exec("INSERT INTO eval_snapshot_document VALUES ('tenant',:x,:x,:s,'tenant','origin-doc')",p);db.exec("INSERT INTO eval_snapshot VALUES ('tenant',:x,'READY',JSON_OBJECT('secret','value'))",p);db.exec("INSERT INTO eval_gold VALUES ('tenant',:x,:s,'tenant','origin-doc',JSON_OBJECT('quote','secret'))",p);db.exec("INSERT INTO eval_case VALUES ('tenant',:x,:x,JSON_OBJECT('q','secret'),JSON_OBJECT('a','secret'),'REVIEWED')",p);db.exec("INSERT INTO eval_dataset_version VALUES ('tenant',:x,'FROZEN')",p);}
 @Test void currentOriginPurgesCurrentAndLegacyLocalEvidence(){seed("evaluation","current");seed("LOCAL_SYNTHETIC","legacy");seed("another-origin","other");var response=deletion.receive("evaluation","tenant","origin-doc");assertEquals(2,response.get("copiesPurged"));for(String id:List.of("current","legacy")){assertEquals("PURGED",db.one("SELECT status FROM document WHERE id=:id",Map.of("id",id)).get("status"));assertEquals("PURGED",db.one("SELECT status FROM eval_dataset_version WHERE id=:id",Map.of("id",id)).get("status"));assertFalse(db.one("SELECT CAST(anchor_json AS VARCHAR) AS anchor FROM eval_gold WHERE case_id=:id",Map.of("id",id)).get("anchor").toString().contains("secret"));}assertEquals("FROZEN",db.one("SELECT status FROM eval_dataset_version WHERE id='other'",Map.of()).get("status"));assertDoesNotThrow(()->deletion.receive("evaluation","tenant","origin-doc"));}
 @Test void remoteOriginDoesNotConsumeLegacyLocalAlias(){seed("LOCAL_SYNTHETIC","legacy");seed("remote","remote-copy");assertEquals(1,deletion.receive("remote","tenant","origin-doc").get("copiesPurged"));assertEquals("LIVE",db.one("SELECT status FROM document WHERE id='legacy'",Map.of()).get("status"));}
}
