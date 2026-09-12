package dev.rag.identity;

import dev.rag.common.*;
import jakarta.servlet.http.*;
import org.springframework.stereotype.Service;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.locks.ReentrantReadWriteLock;
import java.util.function.Supplier;

/** SQL is the authority. Admin is a maintenance capability, never a content bypass. */
@Service
public class AccessPolicy {
 private final Db db;
 private final Map<String,ReentrantReadWriteLock> locks=new ConcurrentHashMap<>();
 public AccessPolicy(Db db){this.db=db;}
 public static byte[] hash(String value){return hash(value.getBytes(StandardCharsets.UTF_8));}
 public static byte[] hash(byte[] value){try{return MessageDigest.getInstance("SHA-256").digest(value);}catch(Exception e){throw new IllegalStateException(e);}}
 public static String cookie(HttpServletRequest r,String name){if(r.getCookies()!=null)for(Cookie c:r.getCookies())if(c.getName().equals(name))return c.getValue();return "";}
 public Actor actor(HttpServletRequest request){
  String token=cookie(request,"RAG_SESSION");
  if(token.isBlank())throw new ApiException(401,"AUTH_REQUIRED","请先登录");
  var row=db.optional("SELECT u.tenant_id,u.id,u.is_admin FROM auth_session s JOIN app_user u ON u.tenant_id=s.tenant_id AND u.id=s.user_id WHERE s.token_hash=:h AND s.revoked_at IS NULL AND s.expires_at>UTC_TIMESTAMP(6) AND u.status='ACTIVE'",Map.of("h",hash(token)))
   .orElseThrow(()->new ApiException(401,"AUTH_REQUIRED","登录状态已失效"));
  return new Actor(row.get("tenant_id").toString(),row.get("id").toString(),Boolean.TRUE.equals(row.get("is_admin"))||"1".equals(row.get("is_admin").toString()));
 }
 public void requireAdmin(Actor a){if(!a.admin())throw new ApiException(403,"OPERATION_FORBIDDEN","需要维护权限");}
 public static String principal(String alias){return "("+alias+".principal_type='ALL' OR "+alias+".user_id=:u OR EXISTS (SELECT 1 FROM group_member gm WHERE gm.tenant_id=:t AND gm.user_id=:u AND gm.group_id="+alias+".group_id))";}
 public static String readable(){return "d.tenant_id=:t AND d.status='LIVE' AND EXISTS (SELECT 1 FROM app_user au WHERE au.tenant_id=:t AND au.id=:u AND au.status='ACTIVE') AND EXISTS (SELECT 1 FROM knowledge_base kb JOIN kb_grant kg ON kg.tenant_id=kb.tenant_id AND kg.kb_id=kb.id WHERE kb.tenant_id=d.tenant_id AND kb.id=d.kb_id AND kb.status='LIVE' AND "+principal("kg")+") AND (d.acl_mode='INHERIT' OR EXISTS (SELECT 1 FROM document_grant dg WHERE dg.tenant_id=d.tenant_id AND dg.document_id=d.id AND "+principal("dg")+"))";}
 public boolean canReadKb(Actor a,String id){return !db.list("SELECT kb.id FROM knowledge_base kb WHERE kb.tenant_id=:t AND kb.id=:id AND kb.status='LIVE' AND EXISTS (SELECT 1 FROM kb_grant kg WHERE kg.tenant_id=kb.tenant_id AND kg.kb_id=kb.id AND "+principal("kg")+")",Db.params("t",a.tenantId(),"u",a.userId(),"id",id)).isEmpty();}
 public boolean canReadDocument(Actor a,String id){return !db.list("SELECT d.id FROM document d WHERE "+readable()+" AND d.id=:id",Db.params("t",a.tenantId(),"u",a.userId(),"id",id)).isEmpty();}
 public void requireDocument(Actor a,String id){if(!canReadDocument(a,id))throw ApiException.missing();}
 public List<String> readableBuilds(Actor a,List<String> kbIds){
  var p=Db.params("t",a.tenantId(),"u",a.userId());String filter="";
  if(kbIds!=null&&!kbIds.isEmpty()){filter=" AND d.kb_id IN (:kb)";p.put("kb",kbIds);}
  var rows=db.list("SELECT d.active_build_id FROM document d WHERE "+readable()+" AND d.active_build_id IS NOT NULL"+filter+" LIMIT 10001",p);
  if(rows.size()>10000)throw new ApiException(422,"SCOPE_TOO_LARGE","请缩小知识库范围");
  return rows.stream().map(r->r.get("active_build_id").toString()).toList();
 }
 public <T>T guard(Actor a,Collection<String> ids,Supplier<T> work){var lock=locks.computeIfAbsent(a.tenantId(),x->new ReentrantReadWriteLock(true)).readLock();lock.lock();try{for(String id:new TreeSet<>(ids))requireDocument(a,id);return work.get();}finally{lock.unlock();}}
 public <T>T write(Actor a,Supplier<T> work){var lock=locks.computeIfAbsent(a.tenantId(),x->new ReentrantReadWriteLock(true)).writeLock();lock.lock();try{return work.get();}finally{lock.unlock();}}
}
