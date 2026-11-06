// 剪映加密草稿解密器（macOS）
// 调用 lvve::EncryptUtils::decrypt：接受加密内容字符串，返回明文字符串
// 用法: ./decrypt_tool <in.json> <out.json>
#include <dlfcn.h>
#include <string>
#include <cstdio>
#include <fstream>
#include <sstream>

// bool decrypt(const std::string& in, std::string out)
// 返回 std::string（hidden first arg 约定，但用真正的 C++ 返回类型即可）
typedef std::string (*DecryptFn)(const std::string&, std::string);

int main(int argc, char** argv) {
    if (argc < 3) {
        printf("usage: %s <in> <out>\n", argv[0]);
        return 1;
    }
    void* h = dlopen(
        "/Applications/VideoFusion-macOS.app/Contents/Frameworks/libvideoeditor.dylib",
        RTLD_GLOBAL | RTLD_LAZY);
    if (!h) {
        printf("dlopen fail: %s\n", dlerror());
        return 1;
    }
    DecryptFn fn = (DecryptFn)dlsym(
        h, "_ZN4lvve12EncryptUtils7decryptERKNSt3__112basic_stringIcNS1_11char_traitsIcEENS1_9allocatorIcEEEES9_");
    if (!fn) {
        printf("dlsym fail: %s\n", dlerror());
        return 1;
    }
    // 读入整个加密文件
    std::ifstream f(argv[1], std::ios::binary);
    std::stringstream ss;
    ss << f.rdbuf();
    std::string in = ss.str();
    std::string out = fn(in, std::string(""));
    if (out.empty()) {
        printf("decrypt returned empty\n");
        return 2;
    }
    std::ofstream o(argv[2], std::ios::binary);
    o << out;
    printf("decrypt ok, out bytes=%zu\n", out.size());
    return 0;
}
