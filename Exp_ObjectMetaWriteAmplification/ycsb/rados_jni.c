#include <jni.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <errno.h>
#include <rados/librados.h>

static void fail(JNIEnv *env, const char *operation, int status) {
    char message[256];
    snprintf(message, sizeof(message), "%s: RADOS status %d", operation, status);
    (*env)->ThrowNew(env, (*env)->FindClass(env, "java/lang/RuntimeException"), message);
}

JNIEXPORT jlong JNICALL Java_lab_RadosBlockDB_openCluster(
        JNIEnv *env, jclass cls, jstring config, jstring id) {
    rados_t cluster = NULL;
    const char *name = (*env)->GetStringUTFChars(env, id, NULL);
    int status = rados_create(&cluster, name);
    (*env)->ReleaseStringUTFChars(env, id, name);
    if (status < 0) { fail(env, "rados_create", status); return 0; }
    const char *path = (*env)->GetStringUTFChars(env, config, NULL);
    status = rados_conf_read_file(cluster, path);
    (*env)->ReleaseStringUTFChars(env, config, path);
    if (status >= 0) { status = rados_conf_set(cluster, "rados_osd_op_timeout", "600"); }
    if (status >= 0) { status = rados_connect(cluster); }
    if (status < 0) {
        rados_shutdown(cluster);
        fail(env, "rados_connect/config", status);
        return 0;
    }
    return (jlong)(intptr_t)cluster;
}

JNIEXPORT void JNICALL Java_lab_RadosBlockDB_closeCluster(JNIEnv *env, jclass cls, jlong handle) {
    rados_shutdown((rados_t)(intptr_t)handle);
}

JNIEXPORT jlong JNICALL Java_lab_RadosBlockDB_openIoctx(
        JNIEnv *env, jclass cls, jlong handle, jstring pool) {
    rados_ioctx_t ioctx;
    const char *name = (*env)->GetStringUTFChars(env, pool, NULL);
    int status = rados_ioctx_create((rados_t)(intptr_t)handle, name, &ioctx);
    (*env)->ReleaseStringUTFChars(env, pool, name);
    if (status < 0) { fail(env, "rados_ioctx_create", status); return 0; }
    return (jlong)(intptr_t)ioctx;
}

JNIEXPORT void JNICALL Java_lab_RadosBlockDB_closeIoctx(JNIEnv *env, jclass cls, jlong handle) {
    rados_ioctx_destroy((rados_ioctx_t)(intptr_t)handle);
}

JNIEXPORT jint JNICALL Java_lab_RadosBlockDB_writeFull(
        JNIEnv *env, jclass cls, jlong handle, jstring key, jbyteArray value, jboolean bypass) {
    const char *name = (*env)->GetStringUTFChars(env, key, NULL);
    jsize size = (*env)->GetArrayLength(env, value);
    jbyte *bytes = (*env)->GetByteArrayElements(env, value, NULL);
    int status;
    if (bypass) {
        rados_write_op_t op = rados_create_write_op();
        rados_write_op_write_full(op, (const char *)bytes, size);
        status = rados_write_op_operate(op, (rados_ioctx_t)(intptr_t)handle, name, NULL,
                     LIBRADOS_OPERATION_IGNORE_CACHE | LIBRADOS_OPERATION_IGNORE_OVERLAY);
        rados_release_write_op(op);
    } else {
        status = rados_write_full((rados_ioctx_t)(intptr_t)handle, name, (const char *)bytes, size);
    }
    (*env)->ReleaseByteArrayElements(env, value, bytes, JNI_ABORT);
    (*env)->ReleaseStringUTFChars(env, key, name);
    return status;
}

JNIEXPORT jbyteArray JNICALL Java_lab_RadosBlockDB_readFull(
        JNIEnv *env, jclass cls, jlong handle, jstring key, jint size, jboolean bypass) {
    char *buffer = malloc((size_t)size);
    if (!buffer) { fail(env, "malloc", -ENOMEM); return NULL; }
    const char *name = (*env)->GetStringUTFChars(env, key, NULL);
    int status;
    size_t actual = 0;
    if (bypass) {
        int rval = 0;
        rados_read_op_t op = rados_create_read_op();
        rados_read_op_read(op, 0, size, buffer, &actual, &rval);
        status = rados_read_op_operate(op, (rados_ioctx_t)(intptr_t)handle, name,
                     LIBRADOS_OPERATION_IGNORE_CACHE | LIBRADOS_OPERATION_IGNORE_OVERLAY);
        if (status >= 0 && rval < 0) { status = rval; }
        rados_release_read_op(op);
    } else {
        status = rados_read((rados_ioctx_t)(intptr_t)handle, name, buffer, size, 0);
        if (status >= 0) { actual = (size_t)status; }
    }
    (*env)->ReleaseStringUTFChars(env, key, name);
    if (status < 0) {
        free(buffer);
        fail(env, "rados_read", status);
        return NULL;
    }
    jbyteArray result = (*env)->NewByteArray(env, (jsize)actual);
    if (result) { (*env)->SetByteArrayRegion(env, result, 0, (jsize)actual, (const jbyte *)buffer); }
    free(buffer);
    return result;
}

JNIEXPORT jint JNICALL Java_lab_RadosBlockDB_remove(
        JNIEnv *env, jclass cls, jlong handle, jstring key) {
    const char *name = (*env)->GetStringUTFChars(env, key, NULL);
    int status = rados_remove((rados_ioctx_t)(intptr_t)handle, name);
    (*env)->ReleaseStringUTFChars(env, key, name);
    return status;
}
