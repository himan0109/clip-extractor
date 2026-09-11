const express = require('express');
const multer = require('multer');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const axios = require('axios');
const { google } = require('googleapis');
require('dotenv').config();

const app = express();
const port = 3001;

function getPythonPath() {
  const venvPython = path.join(__dirname, '.venv', 'bin', 'python3');
  if (fs.existsSync(venvPython)) return venvPython;
  return 'python3';
}

// ── Startup cleanup ──────────────────────────────────────────────────────────
function cleanupDirectories() {
  const foldersToClean = ['uploads', 'output'];
  foldersToClean.forEach(name => {
    const p = path.join(__dirname, name);
    if (!fs.existsSync(p)) { fs.mkdirSync(p, { recursive: true }); return; }
    fs.readdirSync(p).forEach(f => {
      const fp = path.join(p, f);
      if (fs.statSync(fp).isFile()) fs.unlinkSync(fp);
    });
  });
  // leftover temp configs
  try {
    fs.readdirSync(__dirname).forEach(f => {
      if (f.startsWith('temp_clipout_config_') && f.endsWith('.json'))
        try { fs.unlinkSync(path.join(__dirname, f)); } catch {}
    });
  } catch {}
}
cleanupDirectories();

app.use(express.static('public'));
app.use(express.urlencoded({ extended: true }));
app.use(express.json());

// ── Multer storage ───────────────────────────────────────────────────────────
const storage = multer.diskStorage({
  destination: (req, file, cb) => cb(null, path.join(__dirname, 'uploads')),
  filename:    (req, file, cb) => cb(null, `${Date.now()}_${file.originalname}`)
});

const clipoutUpload = multer({
  storage,
  limits: { fileSize: 2000 * 1024 * 1024 },
  fileFilter: (req, file, cb) => {
    if (file.fieldname === 'video') {
      if (file.mimetype.startsWith('video/')) return cb(null, true);
      return cb(new Error('Video file must be a video format'));
    }
    if (file.fieldname === 'csv') {
      const allowed = ['text/csv','application/vnd.ms-excel','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','application/csv','text/plain'];
      const ext = path.extname(file.originalname).toLowerCase();
      if (allowed.includes(file.mimetype) || ['.csv','.xlsx','.xls'].includes(ext)) return cb(null, true);
      return cb(new Error('CSV file must be CSV or Excel format'));
    }
    cb(new Error('Unknown file field'));
  }
});

// ── YouTube credentials ──────────────────────────────────────────────────────
const YOUTUBE_CREDENTIALS_FILE  = path.join(__dirname, 'youtube_credentials.json');
const YOUTUBE_OAUTH_CONFIG_FILE  = path.join(__dirname, 'youtube_oauth_config.json');

function loadYouTubeCredentials() {
  try { if (fs.existsSync(YOUTUBE_CREDENTIALS_FILE)) return JSON.parse(fs.readFileSync(YOUTUBE_CREDENTIALS_FILE, 'utf8')); } catch {}
  return {};
}
function saveYouTubeCredentials(c) {
  try { fs.writeFileSync(YOUTUBE_CREDENTIALS_FILE, JSON.stringify(c, null, 2)); } catch {}
}

function loadYouTubeOAuthFile() {
  try { if (fs.existsSync(YOUTUBE_OAUTH_CONFIG_FILE)) return JSON.parse(fs.readFileSync(YOUTUBE_OAUTH_CONFIG_FILE, 'utf8')); } catch {}
  return {};
}
function loadYouTubeOAuthConfig() {
  const file = loadYouTubeOAuthFile();
  if (file.active?.clientId && file.active?.clientSecret)
    return { clientId: file.active.clientId.trim(), clientSecret: file.active.clientSecret.trim(), redirectUri: file.active.redirectUri || 'http://localhost:3001/youtube/callback' };
  if (file.clientId && file.clientSecret)
    return { clientId: file.clientId.trim(), clientSecret: file.clientSecret.trim(), redirectUri: file.redirectUri || 'http://localhost:3001/youtube/callback' };
  return { clientId: process.env.YOUTUBE_CLIENT_ID || '', clientSecret: process.env.YOUTUBE_CLIENT_SECRET || '', redirectUri: 'http://localhost:3001/youtube/callback' };
}
function saveYouTubeOAuthConfig(active) {
  const file = loadYouTubeOAuthFile();
  fs.writeFileSync(YOUTUBE_OAUTH_CONFIG_FILE, JSON.stringify({ profiles: file.profiles || [], active }, null, 2));
}
function loadYouTubeProfiles() { return Array.isArray(loadYouTubeOAuthFile().profiles) ? loadYouTubeOAuthFile().profiles : []; }
function saveYouTubeProfiles(profiles) {
  const file = loadYouTubeOAuthFile();
  fs.writeFileSync(YOUTUBE_OAUTH_CONFIG_FILE, JSON.stringify({ profiles, active: file.active || null }, null, 2));
}

app.post('/youtube/save-credentials', express.json(), (req, res) => {
  const { clientId, clientSecret } = req.body;
  if (!clientId || !clientSecret) return res.status(400).json({ error: 'Client ID and Client Secret are required' });
  saveYouTubeOAuthConfig({ clientId: clientId.trim(), clientSecret: clientSecret.trim(), redirectUri: 'http://localhost:3001/youtube/callback' });
  res.json({ success: true });
});

app.get('/youtube/credentials', (req, res) => {
  const c = loadYouTubeOAuthConfig();
  res.json({ clientId: c.clientId || '', hasClientSecret: !!c.clientSecret });
});

app.get('/youtube/saved-profiles', (req, res) => res.json({ profiles: loadYouTubeProfiles() }));

app.post('/youtube/save-profile', express.json(), (req, res) => {
  const { name, clientId, clientSecret } = req.body;
  if (!name || !clientId || !clientSecret) return res.status(400).json({ error: 'name, clientId, and clientSecret required' });
  const profiles = loadYouTubeProfiles();
  const idx = profiles.findIndex(p => p.name === name.trim());
  const entry = { name: name.trim(), clientId: clientId.trim(), clientSecret: clientSecret.trim() };
  if (idx >= 0) profiles[idx] = entry; else profiles.push(entry);
  saveYouTubeProfiles(profiles);
  res.json({ success: true });
});

app.delete('/youtube/saved-profile/:name', (req, res) => {
  saveYouTubeProfiles(loadYouTubeProfiles().filter(p => p.name !== decodeURIComponent(req.params.name)));
  res.json({ success: true });
});

app.get('/youtube/auth', (req, res) => {
  const config = loadYouTubeOAuthConfig();
  if (!config.clientId || !config.clientSecret) {
    return res.status(400).send(`<html><body style="font-family:Arial;text-align:center;padding:50px;background:#1a1a1a;color:#fff;"><h1>❌ YouTube Credentials Not Configured</h1><script>setTimeout(()=>{if(window.opener)window.opener.postMessage({type:'youtube_auth_error',message:'Credentials not configured'},'*');window.close();},3000);</script></body></html>`);
  }
  const oauth2Client = new google.auth.OAuth2(config.clientId, config.clientSecret, config.redirectUri);
  const authUrl = oauth2Client.generateAuthUrl({ access_type: 'offline', scope: ['https://www.googleapis.com/auth/youtube'], prompt: 'consent' });
  res.redirect(authUrl);
});

app.get('/youtube/callback', async (req, res) => {
  try {
    const { code } = req.query;
    if (!code) return res.send('<script>window.close();</script>');
    const config = loadYouTubeOAuthConfig();
    const oauth2Client = new google.auth.OAuth2(config.clientId, config.clientSecret, config.redirectUri);
    const { tokens } = await oauth2Client.getToken(code);
    oauth2Client.setCredentials(tokens);
    const youtube = google.youtube({ version: 'v3', auth: oauth2Client });
    const channelResponse = await youtube.channels.list({ part: 'snippet', mine: true });
    if (!channelResponse.data.items?.length) return res.send('<h1>No YouTube channel found</h1>');
    const channel = channelResponse.data.items[0];
    const credentials = loadYouTubeCredentials();
    credentials[channel.id] = { channelId: channel.id, channelTitle: channel.snippet.title, refreshToken: tokens.refresh_token, accessToken: tokens.access_token, expiryDate: tokens.expiry_date, connectedAt: new Date().toISOString() };
    saveYouTubeCredentials(credentials);
    res.send(`<html><body style="font-family:Arial;text-align:center;padding:50px;"><h1>✅ YouTube Channel Connected!</h1><p>${channel.snippet.title}</p><script>setTimeout(()=>{if(window.opener)window.opener.postMessage({type:'youtube_connected',channelId:'${channel.id}',channelTitle:'${channel.snippet.title}'},'*');window.close();},2000);</script></body></html>`);
  } catch (e) {
    res.send(`<html><body style="text-align:center;padding:50px;"><h1>❌ Error</h1><p>${e.message}</p><script>setTimeout(()=>window.close(),3000);</script></body></html>`);
  }
});

app.get('/youtube/channels', (req, res) => {
  const creds = loadYouTubeCredentials();
  res.json({ channels: Object.values(creds).map(c => ({ channelId: c.channelId, channelTitle: c.channelTitle, connectedAt: c.connectedAt })) });
});

app.delete('/youtube/channels/:channelId', (req, res) => {
  const creds = loadYouTubeCredentials();
  if (!creds[req.params.channelId]) return res.status(404).json({ error: 'Channel not found' });
  delete creds[req.params.channelId];
  saveYouTubeCredentials(creds);
  res.json({ success: true });
});

// ── Facebook/Instagram OAuth ─────────────────────────────────────────────────
const FACEBOOK_OAUTH_CONFIG_FILE  = path.join(__dirname, 'facebook_oauth_config.json');
const FACEBOOK_CREDENTIALS_FILE   = path.join(__dirname, 'facebook_credentials.json');

function loadFacebookOAuthConfig() {
  try {
    if (fs.existsSync(FACEBOOK_OAUTH_CONFIG_FILE)) {
      const c = JSON.parse(fs.readFileSync(FACEBOOK_OAUTH_CONFIG_FILE, 'utf8'));
      if (c.appId && c.appSecret) return { appId: c.appId.trim(), appSecret: c.appSecret.trim(), redirectUri: c.redirectUri || 'http://localhost:3001/facebook/callback' };
    }
  } catch {}
  return { appId: process.env.FACEBOOK_APP_ID || '', appSecret: process.env.FACEBOOK_APP_SECRET || '', redirectUri: 'http://localhost:3001/facebook/callback' };
}
function saveFacebookOAuthConfig(c) { fs.writeFileSync(FACEBOOK_OAUTH_CONFIG_FILE, JSON.stringify(c, null, 2)); }
function loadFacebookCredentials() {
  try { if (fs.existsSync(FACEBOOK_CREDENTIALS_FILE)) return JSON.parse(fs.readFileSync(FACEBOOK_CREDENTIALS_FILE, 'utf8')); } catch {}
  return {};
}
function saveFacebookCredentials(c) { fs.writeFileSync(FACEBOOK_CREDENTIALS_FILE, JSON.stringify(c, null, 2)); }

app.post('/facebook/save-credentials', express.json(), (req, res) => {
  const { appId, appSecret } = req.body;
  if (!appId || !appSecret) return res.status(400).json({ error: 'App ID and App Secret required' });
  saveFacebookOAuthConfig({ appId: appId.trim(), appSecret: appSecret.trim(), redirectUri: 'http://localhost:3001/facebook/callback' });
  res.json({ success: true });
});

app.get('/facebook/credentials', (req, res) => {
  const c = loadFacebookOAuthConfig();
  res.json({ appId: c.appId || '', hasAppSecret: !!c.appSecret });
});

app.get('/facebook/auth', (req, res) => {
  const config = loadFacebookOAuthConfig();
  if (!config.appId || !config.appSecret)
    return res.status(400).send(`<html><body style="font-family:Arial;text-align:center;padding:50px;background:#1a1a1a;color:#fff;"><h1>❌ Facebook Credentials Not Configured</h1><script>setTimeout(()=>{if(window.opener)window.opener.postMessage({type:'facebook_auth_error',message:'Credentials not configured'},'*');window.close();},3000);</script></body></html>`);
  const scopes = 'pages_show_list,pages_read_engagement,instagram_basic,instagram_content_publish,business_management';
  const authUrl = `https://www.facebook.com/v18.0/dialog/oauth?client_id=${encodeURIComponent(config.appId)}&redirect_uri=${encodeURIComponent(config.redirectUri)}&scope=${encodeURIComponent(scopes)}&response_type=code&state=${Date.now()}`;
  res.redirect(authUrl);
});

app.get('/facebook/callback', async (req, res) => {
  try {
    const { code, error } = req.query;
    if (error) return res.send(`<html><body style="text-align:center;padding:50px;background:#1a1a1a;color:#fff;"><h1>❌ Authorization Denied</h1><script>setTimeout(()=>{if(window.opener)window.opener.postMessage({type:'facebook_auth_error',message:'Authorization denied'},'*');window.close();},3000);</script></body></html>`);
    if (!code) return res.send('<script>window.close();</script>');
    const config = loadFacebookOAuthConfig();
    const tokenResp = await axios.get('https://graph.facebook.com/v18.0/oauth/access_token', { params: { client_id: config.appId, client_secret: config.appSecret, redirect_uri: config.redirectUri, code } });
    const accessToken = tokenResp.data.access_token;
    const pagesResp = await axios.get('https://graph.facebook.com/v18.0/me/accounts', { params: { access_token: accessToken, fields: 'id,name,access_token,instagram_business_account' } });
    const pages = pagesResp.data.data || [];
    const credentials = loadFacebookCredentials();
    for (const page of pages) {
      credentials[`page_${page.id}`] = { pageId: page.id, pageName: page.name, pageAccessToken: page.access_token, instagramAccountId: null, instagramUsername: null, connectedAt: new Date().toISOString() };
      if (page.instagram_business_account?.id) {
        try {
          const ig = await axios.get(`https://graph.facebook.com/v18.0/${page.instagram_business_account.id}`, { params: { access_token: page.access_token, fields: 'id,username' } });
          credentials[`page_${page.id}`].instagramAccountId = ig.data.id;
          credentials[`page_${page.id}`].instagramUsername  = ig.data.username;
          credentials[`ig_${ig.data.id}`] = { accountId: ig.data.id, username: ig.data.username, pageId: page.id, pageName: page.name, pageAccessToken: page.access_token, connectedAt: new Date().toISOString() };
        } catch {}
      }
    }
    saveFacebookCredentials(credentials);
    res.send(`<html><body style="font-family:Arial;text-align:center;padding:50px;"><h1>✅ Facebook/Instagram Connected!</h1><p>${pages.length} page(s) found.</p><script>setTimeout(()=>{if(window.opener)window.opener.postMessage({type:'facebook_connected',pagesCount:${pages.length}},'*');window.close();},2000);</script></body></html>`);
  } catch (e) {
    res.send(`<html><body style="text-align:center;padding:50px;"><h1>❌ Error</h1><p>${e.message}</p><script>setTimeout(()=>window.close(),5000);</script></body></html>`);
  }
});

app.get('/facebook/pages', (req, res) => {
  const creds = loadFacebookCredentials();
  res.json({ pages: Object.entries(creds).filter(([k]) => k.startsWith('page_')).map(([,c]) => ({ pageId: c.pageId, pageName: c.pageName, hasInstagram: !!c.instagramAccountId, instagramUsername: c.instagramUsername })) });
});

app.get('/instagram/accounts', (req, res) => {
  const creds = loadFacebookCredentials();
  res.json({ accounts: Object.entries(creds).filter(([k]) => k.startsWith('ig_')).map(([,c]) => ({ accountId: c.accountId, username: c.username, pageId: c.pageId, pageName: c.pageName })) });
});

app.delete('/facebook/pages/:pageId', (req, res) => {
  const creds = loadFacebookCredentials();
  const key = `page_${req.params.pageId}`;
  if (!creds[key]) return res.status(404).json({ error: 'Page not found' });
  delete creds[key];
  Object.keys(creds).forEach(k => { if (k.startsWith('ig_') && creds[k].pageId === req.params.pageId) delete creds[k]; });
  saveFacebookCredentials(creds);
  res.json({ success: true });
});

app.delete('/instagram/accounts/:accountId', (req, res) => {
  const creds = loadFacebookCredentials();
  const key = `ig_${req.params.accountId}`;
  if (!creds[key]) return res.status(404).json({ error: 'Account not found' });
  delete creds[key];
  saveFacebookCredentials(creds);
  res.json({ success: true });
});

// ── Clipout routes ───────────────────────────────────────────────────────────
const activeClipoutProcesses = new Map();
const clipoutReviewState = new Map();

app.get('/clipout/backgrounds', (req, res) => {
  try {
    const dir = path.join(__dirname, 'pictures');
    if (!fs.existsSync(dir)) return res.json({ folder: dir, files: [] });
    const allowed = new Set(['.jpg','.jpeg','.png','.webp','.mp4','.mov','.webm']);
    const files = fs.readdirSync(dir).filter(n => fs.statSync(path.join(dir,n)).isFile() && allowed.has(path.extname(n).toLowerCase())).sort();
    res.json({ folder: dir, files });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/start-clipout-shorts', clipoutUpload.any(), async (req, res) => {
  try {
    const files = req.files || [];
    const csvFile = files.find(f => f.fieldname === 'csv');
    const videoFile = files.find(f => f.fieldname === 'video') || null;
    if (!csvFile?.path) return res.status(400).json({ error: 'CSV/Excel file is required' });

    const body = req.body || {};
    const { enable_auto_edit, enable_subtitles, enable_autocut, gap_threshold, transition_style, transition_sfx, loudnorm, zoom_pattern, zoom_percent, max_words, model_name, font_name, font_size, alignment, margin_v, outline, shadow, color_r, color_g, color_b, youtube_channel, instagram_account, facebook_page, use_platforms_from_csv, use_video_path_from_csv, style, picture_folder, picture_background, browser_post_account, youtube_browser_account } = body;

    const review_before_upload = body.review_before_upload === 'true';
    const enable_watermark     = body.enable_watermark === 'on' || body.enable_watermark === 'true';
    const watermark_text       = String(body.watermark_text || '').trim();
    const watermark_position   = String(body.watermark_position || 'bottom-right').trim();
    const watermark_font       = body.watermark_font || 'fire-sans';
    const watermark_size       = body.watermark_size || 'medium';
    const watermark_box        = body.watermark_box === 'off' ? 'off' : 'on';
    const watermark_opacity    = String(body.watermark_opacity || '70');

    let youtube_channels = [];
    if (youtube_channel) {
      if (Array.isArray(youtube_channel)) youtube_channels = youtube_channel.filter(Boolean);
      else if (youtube_channel) youtube_channels = [youtube_channel];
    }

    const usePlatformsFromCsv   = use_platforms_from_csv === 'true';
    const useWatermarkTextFromCsv = body.use_watermark_text_from_csv === 'true';
    const useVideoPathFromCsv   = use_video_path_from_csv === 'true';

    if (!usePlatformsFromCsv && !youtube_channels.length && !instagram_account && !facebook_page && !browser_post_account && !youtube_browser_account)
      return res.status(400).json({ error: 'Please select at least one platform' });

    if (!useVideoPathFromCsv && !videoFile?.path)
      return res.status(400).json({ error: 'Please upload a source video, or enable "Use video path from CSV".' });

    const clipoutId = `${Date.now()}_${Math.random().toString(36).substring(7)}`;

    const config = {
      source_video_path: videoFile?.path || null,
      csv_file_path: csvFile.path,
      edit_settings: {
        enable_autocut: enable_autocut === 'on',
        gap_threshold: gap_threshold || '0.5',
        transition_style: transition_style || '',
        transition_sfx: transition_sfx || '',
        enable_sfx: transition_sfx ? 'on' : 'off',
        loudnorm: loudnorm === 'on',
        zoom_pattern: zoom_pattern || '',
        zoom_percent: zoom_percent || '10',
        enable_watermark, watermark_text, watermark_position, watermark_font, watermark_size, watermark_box, watermark_opacity
      },
      subtitle_settings: {
        max_words: parseInt(max_words) || 1,
        model_name: model_name || 'small',
        font_name: font_name || 'Fira Sans Ultra',
        font_size: parseInt(font_size) || 15,
        alignment: parseInt(alignment) || 2,
        margin_v: parseInt(margin_v) || 75,
        outline: parseInt(outline) || 0,
        shadow: parseInt(shadow) || 2,
        primary_color_hex: `&H00${parseInt(color_b||125).toString(16).padStart(2,'0')}${parseInt(color_g||209).toString(16).padStart(2,'0')}${parseInt(color_r||247).toString(16).padStart(2,'0')}&`
      },
      youtube_channels,
      instagram_account: instagram_account || null,
      facebook_page: facebook_page || null,
      browser_post_account: browser_post_account || null,
      youtube_browser_account: youtube_browser_account || null,
      use_platforms_from_csv: usePlatformsFromCsv,
      use_watermark_text_from_csv: useWatermarkTextFromCsv,
      use_video_path_from_csv: useVideoPathFromCsv,
      style: style === '3' ? 3 : style === '2' ? 2 : 1,
      picture_folder: picture_folder?.trim() || null,
      picture_background: picture_background?.trim() || null,
      review_before_upload,
      clipout_id: clipoutId,
      enable_auto_edit: enable_auto_edit === 'true' || enable_auto_edit === 'on',
      enable_subtitles: enable_subtitles === 'true' || enable_subtitles === 'on',
      enable_hf: body.enable_hf === 'true',
      hf_settings: body.enable_hf === 'true' ? {
        max_words: body.hf_max_words || '6', model_name: body.hf_model_name || 'base.en',
        hf_template: body.hf_template || 'kinetic', animation: body.hf_animation || 'bounce',
        font_name: body.hf_font_name || 'Fira Sans Ultra', font_file: body.hf_font_file || '',
        text_color: body.hf_text_color || '#ffffff', font_size: body.hf_font_size || '52',
        pad_x: body.hf_pad_x || '0', pad_y: body.hf_pad_y || '0',
        hf_align: body.hf_align || 'center', hf_bottom_margin: body.hf_bottom_margin || '150',
        border_radius: body.hf_border_radius || '20', box_color: body.hf_box_color || '#000000',
        sfx_enabled: body.hf_sfx === '1', sfx_type: body.hf_sfx_type || 'click',
        k_context_font: body.hf_kinetic_context_font || '', k_context_color: body.hf_kinetic_context_color || '#ffffff',
        k_emphasis_font: body.hf_kinetic_emphasis_font || '', k_emphasis_color: body.hf_kinetic_emphasis_color || '#FFD700',
        k_show_bg: body.hf_show_bg === '1', k_icons: body.hf_enable_icons === '1', k_shadow: body.hf_show_shadow === '1',
        box_opacity: body.hf_box_opacity || '0',
      } : null
    };

    if (review_before_upload) clipoutReviewState.set(clipoutId, { current: null, decision: {} });

    const configPath = path.join(__dirname, `temp_clipout_config_${clipoutId}.json`);
    fs.writeFileSync(configPath, JSON.stringify(config, null, 2));

    const pythonProcess = spawn(getPythonPath(), ['-u', 'clipout_shorts.py', configPath]);
    const processInfo = { process: pythonProcess, clipoutId, total: 0, completed: 0, status: 'running', excludedChannels: new Set() };
    activeClipoutProcesses.set(clipoutId, processInfo);

    let stdoutData = '';
    pythonProcess.stdout.on('data', data => {
      const text = data.toString();
      stdoutData += text;
      console.log('[clipout]', text);
      const m = text.match(/Processing clip (\d+)\/(\d+):/);
      if (m) { processInfo.total = parseInt(m[2]); processInfo.completed = Math.max(0, parseInt(m[1]) - 1); }
      if (text.includes('JSON_OUTPUT_START')) {
        const s = stdoutData.indexOf('JSON_OUTPUT_START'), e = stdoutData.indexOf('JSON_OUTPUT_END', s);
        if (e > s) try { const j = JSON.parse(stdoutData.substring(s + 'JSON_OUTPUT_START'.length, e).trim()); processInfo.result = j; processInfo.status = 'completed'; processInfo.total = j.total || processInfo.total; processInfo.completed = j.successful || processInfo.completed; } catch {}
      }
      if (text.includes('FATAL_ERROR:')) { const em = text.match(/FATAL_ERROR:\s*(.+)/); if (em) { processInfo.status = 'failed'; processInfo.error = em[1].trim(); } }
    });
    pythonProcess.stderr.on('data', data => console.error('[clipout stderr]', data.toString()));
    pythonProcess.on('close', code => {
      if (code !== 0 && processInfo.status === 'running') { processInfo.status = 'failed'; processInfo.error = `Process exited with code ${code}`; }
      if (processInfo.status === 'running') processInfo.status = 'completed';
      try { if (fs.existsSync(configPath)) fs.unlinkSync(configPath); } catch {}
    });

    res.json({ success: true, clipoutId, message: 'Clipout Shorts processing started' });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/clipout-review/:clipoutId/current', (req, res) => {
  const state = clipoutReviewState.get(req.params.clipoutId);
  if (!state?.current) return res.json({ waiting: false });
  const cur = state.current;
  res.json({ waiting: true, index: cur.index, total: cur.total, title: cur.title, videoUrl: `/clipout-review/${req.params.clipoutId}/video` });
});

app.get('/clipout-review/:clipoutId/video', (req, res) => {
  const state = clipoutReviewState.get(req.params.clipoutId);
  if (!state?.current?.video_path) return res.status(404).send('No review video');
  const resolved = path.resolve(state.current.video_path);
  if (!resolved.startsWith(path.resolve(path.join(__dirname, 'output')))) return res.status(403).send('Forbidden');
  res.sendFile(resolved);
});

app.post('/clipout-review/:clipoutId/notify', express.json(), (req, res) => {
  const state = clipoutReviewState.get(req.params.clipoutId) || { current: null, decision: {} };
  const { index, total, title, video_path } = req.body || {};
  state.current = { index, total, title, video_path };
  clipoutReviewState.set(req.params.clipoutId, state);
  res.json({ success: true });
});

app.post('/clipout-review/:clipoutId/decision', express.json(), (req, res) => {
  const state = clipoutReviewState.get(req.params.clipoutId);
  if (!state) return res.status(404).json({ error: 'clipoutId not found' });
  const { index, decision } = req.body || {};
  if (decision !== 'approve' && decision !== 'skip') return res.status(400).json({ error: 'Invalid decision' });
  state.decision[String(index)] = decision;
  state.current = null;
  clipoutReviewState.set(req.params.clipoutId, state);
  res.json({ success: true });
});

app.get('/clipout-review/:clipoutId/decision', (req, res) => {
  const state = clipoutReviewState.get(req.params.clipoutId);
  if (!state) return res.status(404).json({ error: 'clipoutId not found' });
  const d = state.decision[String(req.query.index)];
  res.json(d ? { decided: true, decision: d } : { decided: false });
});

app.get('/clipout-progress/:clipoutId', (req, res) => {
  const info = activeClipoutProcesses.get(req.params.clipoutId);
  if (!info) return res.status(404).json({ error: 'Clipout process not found' });
  const response = { clipoutId: req.params.clipoutId, total: info.total, completed: info.completed, status: info.status, excludedChannels: Array.from(info.excludedChannels || []) };
  if (info.status === 'completed' && info.result) { response.result = info.result; setTimeout(() => activeClipoutProcesses.delete(req.params.clipoutId), 5000); }
  if (info.status === 'failed' && info.error) response.error = info.error;
  res.json(response);
});

app.post('/stop-clipout-shorts/:clipoutId', (req, res) => {
  const info = activeClipoutProcesses.get(req.params.clipoutId);
  if (!info) return res.status(404).json({ error: 'Clipout process not found' });
  if (!info.process.killed) { info.process.kill('SIGTERM'); info.status = 'stopped'; }
  activeClipoutProcesses.delete(req.params.clipoutId);
  res.json({ success: true, message: 'Clipout Shorts processing stopped' });
});

app.listen(port, '0.0.0.0', () => {
  console.log(`🚀 Clip Extractor running at http://localhost:${port}`);
});
