using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        bool smoke = args.Length == 1 && args[0] == "--smoke-test";
        bool first = true;
        using (var reopen = smoke ? null : new EventWaitHandle(false, EventResetMode.AutoReset,
                   @"Local\RelayDesk.Preview.Reopen", out first))
        {
            if (!first)
            {
                reopen.Set();
                return 0;
            }
            using (var window = new WorkbenchWindow(smoke))
            {
                RegisteredWaitHandle listener = null;
                if (reopen != null)
                {
                    window.Shown += (sender, eventArgs) =>
                    {
                        listener = ThreadPool.RegisterWaitForSingleObject(reopen, (value, timedOut) =>
                        {
                            try { window.BeginInvoke(new Action(window.RestoreWindow)); }
                            catch (InvalidOperationException) { }
                        }, null, Timeout.Infinite, false);
                    };
                }
                try
                {
                    Application.Run(window);
                    return window.ExitCode;
                }
                finally
                {
                    if (listener != null) listener.Unregister(null);
                }
            }
        }
    }
}

internal sealed class WorkbenchWindow : Form
{
    private const string Host = "relaydesk.local";
    private const string Page = "https://" + Host + "/index.html";
    private readonly string directory = AppDomain.CurrentDomain.BaseDirectory;
    private readonly bool smoke;
    private readonly WebView2 view = new WebView2 { Dock = DockStyle.Fill, Visible = false };
    private readonly Label loading = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter };
    private readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer { Interval = 150 };
    private readonly NotifyIcon tray;
    private readonly ContextMenuStrip menu = new ContextMenuStrip();
    private readonly Icon relayIcon;
    private readonly HubBridge hub;
    private bool closing;
    private bool exiting;
    public int ExitCode { get; private set; }

    [DllImport("user32.dll")]
    private static extern bool DestroyIcon(IntPtr icon);

    public WorkbenchWindow(bool smoke)
    {
        this.smoke = smoke;
        ExitCode = smoke ? 1 : 0;
        Text = "Relay Desk Preview";
        Size = new Size(1110, 770);
        MinimumSize = new Size(660, 500);
        AutoScaleMode = AutoScaleMode.Dpi;
        StartPosition = FormStartPosition.CenterScreen;
        if (smoke)
        {
            StartPosition = FormStartPosition.Manual;
            Location = new Point(-16000, -16000);
            ShowInTaskbar = false;
        }
        relayIcon = ReadIcon();
        Icon = relayIcon;
        menu.Items.Add("打开 Relay Desk", null, (sender, args) => RestoreWindow());
        menu.Items.Add("退出 Relay Desk", null, (sender, args) => ExitWorkbench());
        tray = new NotifyIcon { Icon = relayIcon, Text = Text, ContextMenuStrip = menu };
        tray.MouseDoubleClick += (sender, args) => { if (args.Button == MouseButtons.Left) RestoreWindow(); };
        hub = new HubBridge(directory, ReceiveHub, smoke);
        loading.Text = "正在打开 Relay Desk…";
        Controls.Add(view);
        Controls.Add(loading);
        timer.Tick += (sender, args) => hub.Update();
        Shown += Initialize;
    }

    private Icon ReadIcon()
    {
        using (var bitmap = new Bitmap(Path.Combine(directory, "web", "relay-logo.png")))
        using (var small = new Bitmap(bitmap, 48, 48))
        {
            IntPtr handle = small.GetHicon();
            try
            {
                using (var icon = Icon.FromHandle(handle)) return (Icon)icon.Clone();
            }
            finally { DestroyIcon(handle); }
        }
    }

    private async void Initialize(object sender, EventArgs args)
    {
        try
        {
            tray.Visible = !smoke;
            string cache = smoke ? Path.Combine(directory, ".smoke-webview") :
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                             "RelayDeskPreview", "webview");
            var runtime = await CoreWebView2Environment.CreateAsync(null, cache);
            if (closing) return;
            await view.EnsureCoreWebView2Async(runtime);
            if (closing) return;
            var browser = view.CoreWebView2;
            browser.Settings.AreDevToolsEnabled = false;
            browser.Settings.AreDefaultContextMenusEnabled = false;
            browser.Settings.IsStatusBarEnabled = false;
            browser.Settings.IsWebMessageEnabled = true;
            browser.NavigationStarting += (origin, navigation) => navigation.Cancel = !Allowed(navigation.Uri);
            browser.NewWindowRequested += (origin, request) => request.Handled = true;
            browser.WebMessageReceived += OnMessage;
            browser.SetVirtualHostNameToFolderMapping(Host, Path.Combine(directory, "web"),
                CoreWebView2HostResourceAccessKind.DenyCors);
            browser.NavigationCompleted += OnPageReady;
            browser.Navigate(Page);
        }
        catch (Exception failure)
        {
            if (!closing) Fail("无法加载窗口。请检查 Microsoft Edge WebView2 Runtime。" + failure.GetType().Name);
        }
    }

    private async void OnPageReady(object sender, CoreWebView2NavigationCompletedEventArgs args)
    {
        if (closing) return;
        if (!args.IsSuccess)
        {
            Fail("无法打开工作台界面。");
            return;
        }
        loading.Visible = false;
        view.Visible = true;
        if (smoke)
        {
            string ready = await view.CoreWebView2.ExecuteScriptAsync(
                "document.querySelector('#add-account') !== null && document.querySelector('#network-form') !== null && document.querySelector('#diagnostics') !== null");
            if (ready != "true") { ExitWorkbench(); return; }
            timer.Start();
            hub.Request("{\"action\":\"state\"}");
            return;
        }
        timer.Start();
        view.CoreWebView2.PostWebMessageAsJson("{\"kind\":\"host-ready\"}");
    }

    private void ReceiveHub(string message)
    {
        if (!smoke) view.CoreWebView2.PostWebMessageAsJson(message);
        else
        {
            ExitCode = message.Contains("\"kind\": \"hub-result\"") &&
                message.Contains("\"ok\": true") && message.Contains("\"accounts\": []") ? 0 : 1;
            ExitWorkbench();
        }
    }

    private static bool Allowed(string address)
    {
        Uri uri;
        return Uri.TryCreate(address, UriKind.Absolute, out uri) && uri.Scheme == "https" &&
            uri.Host == Host && uri.IsDefaultPort && uri.UserInfo.Length == 0 &&
            uri.AbsolutePath == "/index.html" && uri.Query.Length == 0;
    }

    private void OnMessage(object sender, CoreWebView2WebMessageReceivedEventArgs args)
    {
        if (smoke || closing || !Allowed(args.Source)) return;
        try
        {
            string request = args.TryGetWebMessageAsString();
            if (request != null && request.StartsWith("hub:", StringComparison.Ordinal)) hub.Request(request.Substring(4));
        }
        catch (ArgumentException) { }
    }

    private void Fail(string message)
    {
        ExitCode = 1;
        if (smoke) { Console.Error.WriteLine(message); ExitWorkbench(); }
        else { view.Visible = false; loading.Text = message; loading.Visible = true; }
    }

    public void RestoreWindow()
    {
        if (closing) return;
        Show();
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Activate();
    }

    private void ExitWorkbench()
    {
        exiting = true;
        Close();
    }

    protected override void OnFormClosing(FormClosingEventArgs args)
    {
        base.OnFormClosing(args);
        if (!exiting && args.CloseReason == CloseReason.UserClosing)
        {
            args.Cancel = true;
            Hide();
        }
    }

    protected override void OnFormClosed(FormClosedEventArgs args)
    {
        closing = true;
        timer.Stop();
        timer.Dispose();
        hub.Dispose();
        tray.Visible = false;
        tray.Dispose();
        menu.Dispose();
        base.OnFormClosed(args);
    }

    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);
        if (disposing) relayIcon.Dispose();
    }
}
