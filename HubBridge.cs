using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class HubBridge : IDisposable
{
    private readonly string directory;
    private readonly Action<string> post;
    private readonly bool smoke;
    private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 1048576 };
    private readonly ConcurrentQueue<string> updates = new ConcurrentQueue<string>();
    private Process process;
    private string code;
    private string url;
    private bool receivedResult;
    private bool outputClosed;
    private string copiedCode;
    private readonly Timer clipboardTimer = new Timer { Interval = 60000 };

    public HubBridge(string directory, Action<string> post, bool smoke)
    {
        this.directory = directory;
        this.post = post;
        this.smoke = smoke;
        clipboardTimer.Tick += (sender, args) => ClearCopiedCode();
    }

    public void Request(string request)
    {
        if (request == "open-login")
        {
            if (url != null)
            {
                try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
                catch (System.ComponentModel.Win32Exception) { Notify("无法打开默认浏览器。"); }
            }
            return;
        }
        if (request == "copy-code")
        {
            if (code == null) return;
            try
            {
                Clipboard.SetText(code);
                copiedCode = code;
                clipboardTimer.Stop();
                clipboardTimer.Start();
                Notify("已复制设备代码。");
            }
            catch (System.Runtime.InteropServices.ExternalException) { Notify("剪贴板暂不可用，可以手动复制。"); }
            return;
        }
        if (process != null || request.Length > 4096) return;
        var payload = Decode(request);
        object rawAction;
        if (payload == null || !payload.TryGetValue("action", out rawAction)) return;
        string action = rawAction as string;
        if (action != "add" && action != "login" && action != "remove" && action != "refresh"
            && action != "folder" && action != "state" && action != "settings" && action != "diagnose"
            && action != "network") return;
        try
        {
            process = new Process
            {
                StartInfo = new ProcessStartInfo
                {
                    FileName = PythonLauncher(),
                    Arguments = "-3 -B -X utf8 \"" + Path.Combine(directory, "hub_control.py") + "\"" +
                        (smoke ? " --smoke" : ""),
                    WorkingDirectory = directory,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardInput = true,
                    RedirectStandardOutput = true,
                    StandardOutputEncoding = Encoding.UTF8
                }
            };
            process.OutputDataReceived += (sender, args) => updates.Enqueue(args.Data);
            outputClosed = receivedResult = false;
            process.Start();
            process.BeginOutputReadLine();
            var input = new StringBuilder(request.Length);
            foreach (char c in request)
                if (c < 128) input.Append(c); else input.Append("\\u").Append(((int)c).ToString("x4"));
            process.StandardInput.WriteLine(input);
            process.StandardInput.Close();
        }
        catch (Exception ex)
        {
            if (!(ex is IOException || ex is InvalidOperationException || ex is System.ComponentModel.Win32Exception))
                throw;
            ReleaseProcess();
            Result(false, "无法启动账号操作，请确认 Python 可用。");
        }
    }

    private static string PythonLauncher()
    {
        string native = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "py.exe");
        if (File.Exists(native)) return native;
        string directories = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (string entry in directories.Split(Path.PathSeparator))
        {
            string absolute = Environment.ExpandEnvironmentVariables(entry.Trim().Trim('"'));
            if (!Path.IsPathRooted(absolute)) continue;
            string candidate = Path.Combine(absolute, "py.exe");
            if (File.Exists(candidate)) return candidate;
        }
        throw new IOException("Python launcher not found.");
    }

    private Dictionary<string, object> Decode(string value)
    {
        try { return json.Deserialize<Dictionary<string, object>>(value); }
        catch (ArgumentException) { return null; }
        catch (InvalidOperationException) { return null; }
    }

    public void Update()
    {
        if (process == null) return;
        string line;
        while (updates.TryDequeue(out line))
        {
            if (line == null) { outputClosed = true; continue; }
            var message = Decode(line);
            object kind;
            if (message == null || !message.TryGetValue("kind", out kind)) continue;
            if (Equals(kind, "device"))
            {
                object candidate, candidateCode;
                Uri parsed;
                if (!message.TryGetValue("url", out candidate) || !Uri.TryCreate(candidate as string, UriKind.Absolute, out parsed)
                    || parsed.Scheme != "https" || !parsed.IsDefaultPort || parsed.UserInfo.Length != 0
                    || !(parsed.Host == "openai.com" || parsed.Host.EndsWith(".openai.com", StringComparison.Ordinal))
                    || !message.TryGetValue("code", out candidateCode)
                    || !(candidateCode is string) || !Regex.IsMatch((string)candidateCode, "^[A-Za-z0-9-]{3,32}$"))
                    continue;
                url = parsed.AbsoluteUri;
                code = (string)candidateCode;
                post(line);
            }
            else if (Equals(kind, "hub-result"))
            {
                receivedResult = true;
                pendingResult = line;
            }
            else if (Equals(kind, "diagnostic") || Equals(kind, "network")) post(line);
        }
        if (!process.HasExited || !outputClosed) return;
        ReleaseProcess();
        code = url = null;
        if (receivedResult) post(pendingResult);
        else Result(false, "账号操作已结束，请重试。");
        pendingResult = null;
    }

    private string pendingResult;

    private void Notify(string message)
    {
        post(json.Serialize(new { kind = "device-note", message = message }));
    }

    private void Result(bool ok, string message)
    {
        post(json.Serialize(new { kind = "hub-result", ok = ok, message = message }));
    }

    private void ReleaseProcess()
    {
        if (process != null) process.Dispose();
        process = null;
    }

    private void ClearCopiedCode()
    {
        clipboardTimer.Stop();
        if (copiedCode == null) return;
        try { if (Clipboard.GetText() == copiedCode) Clipboard.Clear(); }
        catch (System.Runtime.InteropServices.ExternalException) { }
        copiedCode = null;
    }

    public void Dispose()
    {
        ClearCopiedCode();
        clipboardTimer.Dispose();
        ReleaseProcess();
    }
}
